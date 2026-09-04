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

# One prepared starting repository per task id, copied into each test's own
# temp tree. Building it took six git subprocesses per test method -- about
# 0.4s on Windows against 0.05s for the copy -- which made this module the
# largest single contributor to suite wall time, and AC-16 runs the whole
# suite inside a bounded acceptance timeout. The copy is a real repository
# with the same HEAD, branch and starting tree, so no test sees a different
# fixture than it did before.
_TEMPLATE_REPOS = {}


def _git_in(root, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(root),
        capture_output=True,
        check=True,
    )


def _write_profile(root, count=4):
    """The validation profile the fixture starts with.

    Shared by the template and by tests that re-write it with a different
    expected test count, so there is one definition of the starting profile.
    """
    stderr = "Ran %d tests in 0.01s\\n\\nOK\\n" % count
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "validation.json").write_text(
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


def _build_starting_repo(root, task_id):
    _git_in(root, "init", "-q", "-b", "main")
    _git_in(root, "config", "user.email", "t@example.com")
    _git_in(root, "config", "user.name", "T")
    (root / "base.txt").write_text("base\n")
    # Part of the starting tree, like the real .ai/validation.json: written
    # before the baseline, so it is pre-existing rather than task-produced.
    _write_profile(root)
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-qm", "base")
    _git_in(root, "checkout", "-q", "-b", "feature/%s" % task_id)


def _template_repo(task_id):
    root = _TEMPLATE_REPOS.get(task_id)

    if root is None:
        root = Path(tempfile.mkdtemp(prefix="wf-template-"))
        _build_starting_repo(root, task_id)
        _TEMPLATE_REPOS[task_id] = root

    return root


def tearDownModule():
    while _TEMPLATE_REPOS:
        _, root = _TEMPLATE_REPOS.popitem()
        shutil.rmtree(root, ignore_errors=True)


class BaselineCase(TaskDirCase):
    """A task workspace inside a real git repo.

    ``enumerate_tree`` shells out to ``git ls-files``, so the tree has to be a
    repository -- and the manifest deliberately honours ``.gitignore``, which
    cannot be tested without one.
    """

    def setUp(self):
        super().setUp()

        template = _template_repo(self.task_id)

        for entry in sorted(template.iterdir()):
            target = self.tmp / entry.name

            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)

    def _git(self, *args):
        _git_in(self.tmp, *args)

    def _plan(self, name="plan.json", modify=(), create=(), criteria=None):
        body = {
            "files_to_modify": [{"path": p, "purpose": "x"} for p in modify],
            "files_to_create": [{"path": p, "purpose": "x"} for p in create],
        }

        if criteria is not None:
            body["acceptance_criteria"] = criteria

        return self.write_plan(name, plan_json(**body))

    def _profile(self, count=4):
        _write_profile(self.tmp, count)

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


@unittest.skipUnless(HAVE_GIT, "git not available")
class ForeignTaskDeltaTests(BaselineCase):
    """One task's work must not become another task's scope violation.

    The delta is still the whole tree -- what changes is attribution. A task's
    delta used to grow for as long as the task stayed open, so two tasks could
    not be in flight at once and an interrupted task made the tree unusable for
    anything else.

    Ownership is structural: another open task's recorded worktree, or its
    ``.ai/tasks/<id>/`` directory. Deliberately not the paths another task's
    plan declares, because that would make one task's plan an authorisation
    input for another task's validation.
    """

    OTHER = "TASK-998"

    def setUp(self):
        super().setUp()
        self._capture()
        self.write_state(status="VALIDATING")
        self._plan(create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")

    def _other_task(self, status="IMPLEMENTING", worktree="sandboxes/TASK-998"):
        """A second task, open, with a recorded worktree inside this tree.

        Recorded somewhere the manifest still enumerates: the conventional
        ``.worktrees/`` location is excluded from the manifest outright, so a
        fixture there would prove nothing about classification.
        """
        directory = self.tmp / ".ai" / "tasks" / self.OTHER
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(
            json.dumps(
                {
                    "task_id": self.OTHER,
                    "status": status,
                    "created_at": "2026-09-01T10:00:00+05:30",
                    "updated_at": "2026-09-01T10:00:00+05:30",
                    "plan_version": 1,
                    "branch": "feature/%s" % self.OTHER,
                    "worktree": worktree,
                    "failure_reason": None,
                }
            )
        )

        if worktree:
            (self.tmp / worktree).mkdir(parents=True, exist_ok=True)

        return directory

    def _scope(self):
        with quiet():
            code = orch.run_validation(self.task_id)

        return code, self._evidence()["diff_scope"]

    def test_other_open_task_paths_are_foreign_not_violations(self):
        self._other_task()
        # Task B's work, in task B's recorded worktree, while task A validates.
        (self.tmp / "sandboxes" / "TASK-998" / "widget.py").write_text("b = 1\n")

        code, scope = self._scope()

        self.assertEqual(code, 0)
        self.assertEqual(
            [entry["path"] for entry in scope["foreign"]],
            ["sandboxes/TASK-998/widget.py"],
        )
        self.assertEqual(scope["foreign"][0]["owner"], self.OTHER)
        self.assertNotIn("sandboxes/TASK-998/widget.py", scope["violations"])
        self.assertEqual(scope["violations"], [])

        # Task B's own task directory is owned structurally too, and no plan of
        # task B's was consulted to establish either: B has an approved plan
        # here declaring a file, and it makes no difference to A's result.
        mine, foreign = orch.classify_foreign_paths(
            self.task_id,
            [
                ".ai/tasks/%s/implementation.md" % self.OTHER,
                "sandboxes/TASK-998/nested/deep.py",
                "widget.py",
            ],
        )

        self.assertEqual(mine, ["widget.py"])
        self.assertEqual(
            sorted(entry["path"] for entry in foreign),
            [
                ".ai/tasks/%s/implementation.md" % self.OTHER,
                "sandboxes/TASK-998/nested/deep.py",
            ],
        )

    def test_ownership_respects_path_component_boundaries(self):
        """An over-broad prefix would hide a real violation."""
        self._other_task()

        mine, foreign = orch.classify_foreign_paths(
            self.task_id,
            ["sandboxes/TASK-9980/x.py", "sandboxes/TASK-998-notes.py"],
        )

        self.assertEqual(foreign, [])
        self.assertEqual(
            mine, ["sandboxes/TASK-9980/x.py", "sandboxes/TASK-998-notes.py"]
        )

    def test_a_completed_task_owns_nothing(self):
        """Its paths are ordinary tree contents again."""
        self._other_task(status="COMPLETED")
        (self.tmp / "sandboxes" / "TASK-998" / "widget.py").write_text("b = 1\n")

        code, scope = self._scope()

        self.assertNotEqual(code, 0)
        self.assertIn("sandboxes/TASK-998/widget.py", scope["violations"])
        self.assertEqual(scope["foreign"], [])

    def test_another_tasks_plan_is_never_read(self):
        """The accepted residual case, asserted so it stays deliberate.

        A concurrent open task with **no** recorded worktree that edits shared
        source is not structurally attributable, so its change still lands in
        this task's delta as a violation. Subtracting the paths task B's plan
        declares would fix that and would also let one task's plan widen a
        scope check it was never reviewed against. This is the trade decision 2
        made, and it is recorded here rather than discovered later.
        """
        directory = self._other_task(worktree=None)
        (directory / "plan.json").write_text(
            plan_json(
                files_to_modify=[{"path": "base.txt", "purpose": "task B's"}]
            )
        )
        (self.tmp / "base.txt").write_text("edited by task B\n")

        code, scope = self._scope()

        self.assertNotEqual(code, 0)
        self.assertIn("base.txt", scope["violations"])
        self.assertEqual(scope["foreign"], [])


@unittest.skipUnless(HAVE_GIT, "git not available")
class CommittedEvidenceTests(BaselineCase):
    """Committed corroboration starts at the baseline, not at the fork point.

    ``resolve_diff_base`` returns merge-base(main, HEAD). On a feature branch
    several commits ahead that is the fork point, so ``committed_changed``
    listed everything the branch had ever carried -- 74 files for TASK-007,
    none of which that task produced. Non-blocking, but noise shaped like
    evidence.
    """

    def setUp(self):
        super().setUp()

        # Several commits beyond the fork, none of them this task's work.
        for name in ("history_one.txt", "history_two.txt"):
            (self.tmp / name).write_text("%s\n" % name)
            self._git("add", "-A")
            self._git("commit", "-qm", "branch work: %s" % name)

        self.write_state(status="VALIDATING")
        self._plan(create=("widget.py",))

    def test_committed_changes_begin_at_baseline_head(self):
        baseline = self._capture()
        head = baseline["git"]["head"]

        # The fixture really is several commits past the fork, so the two bases
        # are different questions with different answers.
        self.assertNotEqual(head, orch.resolve_diff_base())

        (self.tmp / "widget.py").write_text("w = 1\n")
        # Only the source file: `add -A` would sweep the task's own evidence
        # into the commit, where it would look like task-produced source.
        self._git("add", "widget.py")
        self._git("commit", "-qm", "the task's own commit")

        with quiet():
            code = orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(code, 0)
        self.assertEqual(scope["base"], head)
        self.assertEqual(scope["committed_changed"], ["widget.py"])
        # The branch's earlier history is not this task's evidence.
        self.assertNotIn("history_one.txt", scope["committed_changed"])
        self.assertNotIn("history_two.txt", scope["committed_changed"])
        self.assertNotIn("base.txt", scope["committed_changed"])

    def test_missing_baseline_head_omits_committed_changed(self):
        """Manifests written before this existed simply do not have it.

        Omitting the evidence is honest; substituting merge-base is what this
        change removed.
        """
        self.write_baseline()
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(code, 0)
        self.assertIsNone(orch.baseline_head(orch.load_baseline(self.task_id)))
        self.assertNotIn("committed_changed", scope)
        self.assertNotIn("base", scope)
        self.assertIn("no git.head", scope["committed_evidence_omitted"])


@unittest.skipUnless(HAVE_GIT, "git not available")
class RecordedWorktreeDeltaTests(BaselineCase):
    """The recorded worktree is where the task's tree evidence comes from."""

    def setUp(self):
        super().setUp()

        self.worktree = self.tmp / ".worktrees" / self.task_id
        self._git(
            "worktree", "add", "-q", str(self.worktree), "-b",
            "wt/%s" % self.task_id,
        )
        self.write_state(
            status="VALIDATING", worktree=".worktrees/%s" % self.task_id
        )
        self._plan(create=("widget.py",))

    def test_git_and_delta_evidence_use_recorded_worktree(self):
        # The task's real state, so capture roots itself where the task runs.
        baseline = orch.capture_baseline(
            self.task_id, orch.load_state(self.task_id)[1]
        )

        # Git corroboration is the worktree's, not the checkout's: the two are
        # on different branches on purpose.
        self.assertEqual(baseline["git"]["head"], orch.git_head(self.worktree))
        self.assertEqual(baseline["git"]["branch"], "wt/%s" % self.task_id)
        self.assertNotEqual(orch.current_branch(), baseline["git"]["branch"])
        # The evidence itself stays single-homed in the orchestrator checkout.
        self.assertTrue((self.task_path / "baseline.json").is_file())

        (self.worktree / "widget.py").write_text("w = 1\n")
        # A change in the checkout is not this task's work: the task runs in
        # its worktree, so the delta must not see this at all.
        (self.tmp / "decoy.py").write_text("d = 1\n")

        delta = orch.compute_task_delta(
            baseline, orch.execution_root(self.task_id)
        )

        self.assertEqual(delta["created"], ["widget.py"])
        self.assertNotIn("decoy.py", delta["created"])

        with quiet():
            code = orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(code, 0)
        self.assertEqual(scope["changed"], ["widget.py"])
        self.assertEqual(scope["violations"], [])


@unittest.skipUnless(HAVE_GIT, "git not available")
class LegacyTaskRootTests(BaselineCase):
    """Every task that exists today has ``"worktree": null``.

    Including TASK-008 itself. The orchestrator checkout is the fallback for
    both launching and evidence, so those tasks stay drivable.
    """

    def setUp(self):
        super().setUp()
        self.write_state(status="VALIDATING", worktree=None)
        self._plan(create=("widget.py",))

    def test_task_without_worktree_uses_orchestrator_checkout(self):
        self.assertEqual(
            orch.execution_root(self.task_id).resolve(), self.tmp.resolve()
        )

        baseline = self._capture()

        self.assertIn("base.txt", baseline["entries"])
        self.assertEqual(baseline["git"]["head"], orch.git_head())

        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        evidence = self._evidence()

        self.assertEqual(code, 0)
        self.assertEqual(evidence["task_delta"]["created"], ["widget.py"])
        self.assertEqual(evidence["diff_scope"]["violations"], [])
        # Readable afterwards, which is the other half of "remains drivable".
        self.assertEqual(
            orch.load_baseline(self.task_id)["baseline_sha256"],
            baseline["baseline_sha256"],
        )


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
