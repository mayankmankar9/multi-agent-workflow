"""Phase 2 - parallel reviewers, failure classifier, clarifier, plan critic.

The theme: the maker must not be the checker, and a checker that did not run
must never be reported as a checker that found nothing. Phase 0 deleted three
fixed-string review files precisely because they asserted absences nobody had
verified; this phase earns those sections back by making a real read-only agent
produce them, and by recording "did not run" as itself.
"""

import json
import os
import unittest
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

AGENTS = REPO_ROOT / ".claude" / "agents"

READONLY_AGENTS = (
    "reviewer-correctness",
    "reviewer-security",
    "reviewer-performance",
    "failure-classifier",
    "clarifier",
    "plan-critic",
)


def declared_tools(name):
    text = (AGENTS / (name + ".md")).read_text()
    frontmatter = text.split("---", 2)[1]

    for line in frontmatter.splitlines():
        if line.startswith("tools:"):
            return {
                part.strip()
                for part in line.split(":", 1)[1].split(",")
                if part.strip()
            }

    return set()


def agent_reply(payload):
    """A fake claude invocation returning a JSON payload."""

    def fake_run(argv, **kwargs):
        body = payload(argv) if callable(payload) else payload
        return mock.Mock(returncode=0, stdout=body, stderr="")

    return fake_run


class ReadOnlyAgentTests(unittest.TestCase):
    """Checkers must not be able to edit what they are checking."""

    def test_every_checker_agent_exists(self):
        for name in READONLY_AGENTS:
            self.assertTrue((AGENTS / (name + ".md")).is_file(), name)

    def test_no_checker_declares_write_tools(self):
        for name in READONLY_AGENTS:
            tools = declared_tools(name)

            self.assertNotIn("Edit", tools, name)
            self.assertNotIn("Write", tools, name)
            self.assertNotIn("Bash", tools, name)

    def test_readonly_tool_string_grants_no_writes(self):
        granted = set(orch.READONLY_TOOLS.split(","))

        self.assertEqual(granted & {"Edit", "Write", "Bash"}, set())

    def test_reviewers_are_told_not_to_assert_absences(self):
        """The exact failure mode Phase 0 removed must be forbidden in prompt."""
        for dimension in orch.REVIEW_DIMENSIONS:
            body = (AGENTS / ("reviewer-%s.md" % dimension)).read_text()

            self.assertIn("Never assert an absence", body)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(orch.extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_strips_markdown_fence(self):
        text = "```json\n{\"a\": 1}\n```"

        self.assertEqual(json.loads(orch.extract_json_object(text)), {"a": 1})

    def test_strips_prose_preamble_and_trailer(self):
        text = 'Here you go:\n{"a": 1}\nHope that helps.'

        self.assertEqual(json.loads(orch.extract_json_object(text)), {"a": 1})

    def test_handles_nested_objects(self):
        text = 'x {"a": {"b": [1, 2]}} y'

        self.assertEqual(
            json.loads(orch.extract_json_object(text)), {"a": {"b": [1, 2]}}
        )

    def test_returns_none_without_braces(self):
        self.assertIsNone(orch.extract_json_object("no json here"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(orch.extract_json_object(""))


class StructuredAgentTests(unittest.TestCase):
    def test_invokes_the_agent_read_only(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            data, error = orch.run_structured_agent("some-agent", "p", 10)

        self.assertIsNone(error)
        self.assertEqual(data, {"ok": True})

        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "some-agent")
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertNotIn("Write", allowed)
        self.assertNotIn("Edit", allowed)

    def test_missing_binary_is_an_error_not_a_crash(self):
        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            data, error = orch.run_structured_agent("a", "p", 10)

        self.assertIsNone(data)
        self.assertIn("not found", error)

    def test_timeout_is_an_error(self):
        with mock.patch.object(
            orch.subprocess,
            "run",
            side_effect=orch.subprocess.TimeoutExpired(cmd="claude", timeout=1),
        ):
            data, error = orch.run_structured_agent("a", "p", 10)

        self.assertIsNone(data)
        self.assertIn("timed out", error)

    def test_nonzero_exit_is_an_error(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=2, stdout="", stderr="boom")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            data, error = orch.run_structured_agent("a", "p", 10)

        self.assertIsNone(data)
        self.assertIn("exited 2", error)

    def test_malformed_json_object_is_an_error(self):
        with mock.patch.object(
            orch.subprocess, "run", side_effect=agent_reply('{"a": }')
        ):
            data, error = orch.run_structured_agent("a", "p", 10)

        self.assertIsNone(data)
        self.assertIn("invalid JSON", error)

    def test_response_with_no_object_is_an_error(self):
        """A bare array or prose has no object to extract."""
        for body in ("[1, 2]", "I could not complete the review.", ""):
            with mock.patch.object(
                orch.subprocess, "run", side_effect=agent_reply(body)
            ):
                data, error = orch.run_structured_agent("a", "p", 10)

            self.assertIsNone(data)
            self.assertIn("no JSON object", error)

    def test_truncated_response_is_an_error(self):
        """A response cut off mid-object must not be treated as findings."""
        with mock.patch.object(
            orch.subprocess,
            "run",
            side_effect=agent_reply('{"dimension": "security", "findings": ['),
        ):
            data, error = orch.run_structured_agent("a", "p", 10)

        self.assertIsNone(data)
        self.assertIsNotNone(error)


class ValidateFindingsTests(unittest.TestCase):
    def _findings(self, **overrides):
        data = {
            "dimension": "security",
            "findings": [
                {
                    "severity": "high",
                    "title": "shell injection",
                    "detail": "user input reaches a shell",
                }
            ],
        }
        data.update(overrides)
        return data

    def test_accepts_valid_output(self):
        self.assertEqual(orch.validate_findings(self._findings(), "security"), [])

    def test_accepts_an_empty_finding_list(self):
        """Looked and found nothing is valid, and distinct from not looking."""
        self.assertEqual(
            orch.validate_findings(self._findings(findings=[]), "security"), []
        )

    def test_rejects_a_dimension_mismatch(self):
        problems = orch.validate_findings(self._findings(), "correctness")

        self.assertTrue(any("dimension" in p for p in problems))

    def test_rejects_an_unknown_severity(self):
        data = self._findings(
            findings=[{"severity": "critical", "title": "t", "detail": "d"}]
        )
        problems = orch.validate_findings(data, "security")

        self.assertTrue(any("severity" in p for p in problems))

    def test_rejects_missing_title_or_detail(self):
        data = self._findings(findings=[{"severity": "low"}])
        problems = orch.validate_findings(data, "security")

        self.assertTrue(any("title" in p for p in problems))
        self.assertTrue(any("detail" in p for p in problems))

    def test_rejects_non_list_findings(self):
        problems = orch.validate_findings(
            self._findings(findings="nope"), "security"
        )

        self.assertTrue(any("must be a list" in p for p in problems))


class RunReviewersTests(TaskDirCase):
    def _reply_per_dimension(self, mapping):
        def fake_run(argv, **kwargs):
            # review_prompt resolves the diff base via git first.
            if worker_name(argv) != "claude":
                return mock.Mock(returncode=0, stdout="", stderr="")

            agent = argv[argv.index("--agent") + 1]
            dimension = agent.replace("reviewer-", "")
            return mapping[dimension]

        return fake_run

    def test_runs_every_dimension(self):
        _, state = self.write_state()
        self.write_plan()

        def reply(dimension):
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"dimension": dimension, "findings": []}),
                stderr="",
            )

        mapping = {d: reply(d) for d in orch.REVIEW_DIMENSIONS}

        with mock.patch.object(
            orch.subprocess, "run", side_effect=self._reply_per_dimension(mapping)
        ):
            results = orch.run_reviewers(self.task_id, state)

        self.assertEqual(set(results), set(orch.REVIEW_DIMENSIONS))
        for dimension in orch.REVIEW_DIMENSIONS:
            self.assertTrue(results[dimension]["ran"])

    def test_a_failed_dimension_is_recorded_as_not_run(self):
        """Never conflate 'did not run' with 'found nothing'."""
        _, state = self.write_state()
        self.write_plan()

        mapping = {
            "correctness": mock.Mock(
                returncode=0,
                stdout=json.dumps({"dimension": "correctness", "findings": []}),
                stderr="",
            ),
            "security": mock.Mock(returncode=3, stdout="", stderr="crashed"),
            "performance": mock.Mock(
                returncode=0, stdout="not json at all", stderr=""
            ),
        }

        with mock.patch.object(
            orch.subprocess, "run", side_effect=self._reply_per_dimension(mapping)
        ):
            results = orch.run_reviewers(self.task_id, state)

        self.assertTrue(results["correctness"]["ran"])
        self.assertFalse(results["security"]["ran"])
        self.assertIn("exited 3", results["security"]["error"])
        self.assertFalse(results["performance"]["ran"])
        self.assertNotIn("findings", results["security"])

    def test_nonconforming_output_is_not_run(self):
        _, state = self.write_state()
        self.write_plan()

        mapping = {
            d: mock.Mock(
                returncode=0,
                stdout=json.dumps({"dimension": "wrong", "findings": []}),
                stderr="",
            )
            for d in orch.REVIEW_DIMENSIONS
        }

        with mock.patch.object(
            orch.subprocess, "run", side_effect=self._reply_per_dimension(mapping)
        ):
            results = orch.run_reviewers(self.task_id, state)

        for dimension in orch.REVIEW_DIMENSIONS:
            self.assertFalse(results[dimension]["ran"])
            self.assertIn("did not conform", results[dimension]["error"])


class SummariseReviewsTests(unittest.TestCase):
    def test_tags_findings_with_their_dimension(self):
        results = {
            "security": {
                "ran": True,
                "findings": [
                    {"severity": "low", "title": "t", "detail": "d"}
                ],
            }
        }
        summary = orch.summarise_reviews(results)

        self.assertEqual(summary["findings"][0]["dimension"], "security")

    def test_high_severity_is_blocking(self):
        results = {
            "correctness": {
                "ran": True,
                "findings": [
                    {"severity": "high", "title": "bug", "detail": "d"},
                    {"severity": "low", "title": "nit", "detail": "d"},
                ],
            }
        }
        summary = orch.summarise_reviews(results)

        self.assertEqual(len(summary["blocking"]), 1)
        self.assertEqual(summary["blocking"][0]["title"], "bug")

    def test_failed_dimensions_are_listed(self):
        results = {
            "security": {"ran": False, "error": "crashed"},
            "correctness": {"ran": True, "findings": []},
        }
        summary = orch.summarise_reviews(results)

        self.assertEqual(summary["dimensions_ran"], ["correctness"])
        self.assertIn("security", summary["dimensions_failed"])


class RunReviewIntegrationTests(TaskDirCase):
    def _run_review(self, mapping):
        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                agent = argv[argv.index("--agent") + 1]
                return mapping[agent.replace("reviewer-", "")]

            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_review(self.task_id)

        return code, out.getvalue()

    def _clean(self):
        return {
            d: mock.Mock(
                returncode=0,
                stdout=json.dumps({"dimension": d, "findings": []}),
                stderr="",
            )
            for d in orch.REVIEW_DIMENSIONS
        }

    def test_clean_review_reaches_pr_ready(self):
        self.write_state(status="VALIDATED")
        self.write_plan()

        code, _ = self._run_review(self._clean())

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "PR_READY")

    def test_high_severity_finding_blocks_pr_ready(self):
        self.write_state(status="VALIDATED")
        self.write_plan()

        mapping = self._clean()
        mapping["security"] = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "dimension": "security",
                    "findings": [
                        {
                            "severity": "high",
                            "title": "secret committed",
                            "detail": "token in config",
                        }
                    ],
                }
            ),
            stderr="",
        )

        code, out = self._run_review(mapping)

        self.assertEqual(code, 1)
        self.assertEqual(self.read_state()["status"], "FAILED")
        self.assertIn("secret committed", out)

    def test_writes_the_findings_artifact(self):
        self.write_state(status="VALIDATED")
        self.write_plan()

        self._run_review(self._clean())

        data = json.loads((self.task_path / "review-findings.json").read_text())

        self.assertEqual(set(data["dimensions"]), set(orch.REVIEW_DIMENSIONS))

    def test_records_a_review_event(self):
        self.write_state(status="VALIDATED")
        self.write_plan()

        self._run_review(self._clean())

        events = [e for e in self.events() if e["event"] == "REVIEW_COMPLETED"]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["blocking"], 0)

    def test_requires_validated(self):
        self.write_state(status="IMPLEMENTING")

        with quiet() as out:
            code = orch.run_review(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("review requires VALIDATED", out.getvalue())


class ClassifierTests(TaskDirCase):
    def setUp(self):
        super().setUp()
        self._prev = os.environ.pop("ORCHESTRATOR_DISABLE_CLASSIFIER", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("ORCHESTRATOR_DISABLE_CLASSIFIER", None)
        else:
            os.environ["ORCHESTRATOR_DISABLE_CLASSIFIER"] = self._prev

    def test_deterministic_fallback_is_effectively_constant(self):
        """Documenting audit 2.3: none of the real reasons ever matched."""
        for reason in (
            "Claude implementation exited with code 1",
            "Validation command failed with exit code 1",
            "Codex replanning failed",
        ):
            self.assertEqual(orch.classify_failure(reason), "CLAUDE_FIX")

    def test_agent_verdict_is_used(self):
        _, state = self.write_state(
            status="FAILED", failure_reason="Validation failed"
        )
        self.write_plan()

        reply = agent_reply(
            json.dumps(
                {
                    "category": "CODEX_REPLAN",
                    "confidence": "high",
                    "rationale": "the plan names a module that cannot exist",
                }
            )
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=reply):
            route, detail = orch.classify_failure_with_agent(self.task_id, state)

        self.assertEqual(route, "CODEX_REPLAN")
        self.assertEqual(detail["source"], "agent")
        self.assertEqual(detail["confidence"], "high")

    def test_unavailable_agent_falls_back(self):
        _, state = self.write_state(
            status="FAILED", failure_reason="Validation failed"
        )

        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            route, detail = orch.classify_failure_with_agent(self.task_id, state)

        self.assertEqual(route, "CLAUDE_FIX")
        self.assertEqual(detail["source"], "deterministic")
        self.assertIn("unavailable", detail["note"])

    def test_invalid_agent_output_falls_back(self):
        _, state = self.write_state(
            status="FAILED", failure_reason="Validation failed"
        )

        reply = agent_reply(json.dumps({"category": "NONSENSE"}))

        with mock.patch.object(orch.subprocess, "run", side_effect=reply):
            route, detail = orch.classify_failure_with_agent(self.task_id, state)

        self.assertEqual(route, "CLAUDE_FIX")
        self.assertEqual(detail["source"], "deterministic")
        self.assertIn("invalid", detail["note"])

    def test_environment_can_disable_the_classifier(self):
        os.environ["ORCHESTRATOR_DISABLE_CLASSIFIER"] = "1"
        _, state = self.write_state(
            status="FAILED", failure_reason="Validation failed"
        )

        def explode(argv, **kwargs):
            raise AssertionError("classifier should not have been invoked")

        with mock.patch.object(orch.subprocess, "run", side_effect=explode):
            route, detail = orch.classify_failure_with_agent(self.task_id, state)

        self.assertEqual(route, "CLAUDE_FIX")
        self.assertIn("disabled", detail["note"])

    def test_route_failure_records_the_classifier_metadata(self):
        self.write_state(status="FAILED", failure_reason="Validation failed")
        self.write_plan()

        reply = agent_reply(
            json.dumps(
                {
                    "category": "CLAUDE_FIX",
                    "confidence": "medium",
                    "rationale": "test_widget asserts the wrong value",
                }
            )
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=reply):
            with quiet():
                orch.route_failure(self.task_id)

        routed = [e for e in self.events() if e["event"] == "FAILURE_ROUTED"]

        self.assertEqual(routed[0]["classifier"], "agent")
        self.assertEqual(routed[0]["confidence"], "medium")
        self.assertIn("test_widget", routed[0]["rationale"])


class ValidateClassificationTests(unittest.TestCase):
    def test_accepts_a_valid_verdict(self):
        self.assertEqual(
            orch.validate_classification(
                {
                    "category": "CLAUDE_FIX",
                    "confidence": "low",
                    "rationale": "because",
                }
            ),
            [],
        )

    def test_rejects_an_unknown_category(self):
        problems = orch.validate_classification(
            {"category": "REWRITE", "confidence": "low", "rationale": "x"}
        )

        self.assertTrue(any("category" in p for p in problems))

    def test_rejects_a_missing_rationale(self):
        problems = orch.validate_classification(
            {"category": "CLAUDE_FIX", "confidence": "low"}
        )

        self.assertTrue(any("rationale" in p for p in problems))


class ClarificationTests(TaskDirCase):
    def test_writes_questions_and_blocks_advance(self):
        self.write_state(status="ANALYZING")
        self.write_requirement("Add caching.")

        reply = agent_reply(
            json.dumps(
                {
                    "questions": [
                        {
                            "id": "Q-1",
                            "question": "In-memory or on-disk cache?",
                            "why": "different files and eviction",
                            "options": ["in-memory", "on-disk"],
                        }
                    ]
                }
            )
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=reply):
            with quiet():
                self.assertEqual(orch.run_clarify(self.task_id), 0)

        self.assertEqual(len(orch.unanswered_questions(self.task_id)), 1)

        with quiet() as out:
            self.assertEqual(orch.advance_bookkeeping_state(self.task_id), 1)

        self.assertIn("unanswered clarifying question", out.getvalue())
        self.assertEqual(self.read_state()["status"], "ANALYZING")

    def test_answering_unblocks_advance(self):
        self.write_state(status="ANALYZING")
        self.write_requirement()
        orch.clarifications_file(self.task_id).write_text(
            json.dumps(
                {
                    "questions": [
                        {"id": "Q-1", "question": "which?", "answer": "on-disk"}
                    ]
                }
            )
        )

        self.assertEqual(orch.unanswered_questions(self.task_id), [])

        with quiet():
            self.assertEqual(orch.advance_bookkeeping_state(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "PLANNING")

    def test_zero_questions_is_a_valid_answer(self):
        self.write_state(status="ANALYZING")
        self.write_requirement()

        with mock.patch.object(
            orch.subprocess,
            "run",
            side_effect=agent_reply(json.dumps({"questions": []})),
        ):
            with quiet() as out:
                self.assertEqual(orch.run_clarify(self.task_id), 0)

        self.assertIn("No clarifying questions", out.getvalue())
        self.assertEqual(orch.unanswered_questions(self.task_id), [])

    def test_unavailable_clarifier_does_not_block(self):
        self.write_state(status="ANALYZING")
        self.write_requirement()

        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with quiet():
                self.assertEqual(orch.run_clarify(self.task_id), 0)

        skipped = [
            e for e in self.events() if e["event"] == "CLARIFICATION_SKIPPED"
        ]
        self.assertEqual(len(skipped), 1)

    def test_does_not_re_ask_when_questions_exist(self):
        self.write_state(status="ANALYZING")
        self.write_requirement()
        orch.clarifications_file(self.task_id).write_text(
            json.dumps({"questions": [{"id": "Q-1", "question": "x"}]})
        )

        def explode(argv, **kwargs):
            raise AssertionError("clarifier should not have been re-invoked")

        with mock.patch.object(orch.subprocess, "run", side_effect=explode):
            with quiet() as out:
                self.assertEqual(orch.run_clarify(self.task_id), 0)

        self.assertIn("already has", out.getvalue())

    def test_refuses_after_planning_has_started(self):
        self.write_state(status="AWAITING_APPROVAL")

        with quiet() as out:
            self.assertEqual(orch.run_clarify(self.task_id), 1)

        self.assertIn("before planning", out.getvalue())

    def test_answers_reach_the_planning_prompt(self):
        self.write_state(status="PLANNING")
        orch.clarifications_file(self.task_id).write_text(
            json.dumps(
                {
                    "questions": [
                        {
                            "id": "Q-1",
                            "question": "In-memory or on-disk?",
                            "answer": "on-disk, under .cache/",
                        }
                    ]
                }
            )
        )

        context = orch.clarification_context(self.task_id)

        self.assertIn("on-disk, under .cache/", context)
        self.assertIn("Q-1", context)

    def test_unanswered_questions_are_not_sent_as_context(self):
        self.write_state(status="PLANNING")
        orch.clarifications_file(self.task_id).write_text(
            json.dumps(
                {"questions": [{"id": "Q-1", "question": "x", "answer": "  "}]}
            )
        )

        self.assertEqual(orch.clarification_context(self.task_id), "")

    def test_run_driver_stops_at_the_clarification_gate(self):
        self.write_state(status="ANALYZING")
        self.write_requirement()
        orch.clarifications_file(self.task_id).write_text(
            json.dumps({"questions": [{"id": "Q-1", "question": "x"}]})
        )

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertIn("needs a developer decision", out.getvalue())
        self.assertEqual(self.read_state()["status"], "ANALYZING")


class PlanCriticTests(TaskDirCase):
    def _plan_then_critic(self, critic_replies):
        """Fake codex writing a plan, and a critic replying per round."""
        state = {"rounds": 0}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "codex":
                out = argv[argv.index("--output-last-message") + 1]
                orch.Path(out).write_text(plan_json())
                return mock.Mock(returncode=0, stdout="", stderr="")

            reply = critic_replies[min(state["rounds"], len(critic_replies) - 1)]
            state["rounds"] += 1
            return reply

        return fake_run, state

    def _critic(self, verdict, severity=None):
        issues = (
            [{"severity": severity, "issue": "AC-1 cannot fail",
              "suggestion": "use a real check"}]
            if severity
            else []
        )
        return mock.Mock(
            returncode=0,
            stdout=json.dumps({"verdict": verdict, "issues": issues}),
            stderr="",
        )

    def test_passing_critic_plans_once(self):
        self.write_state(status="PLANNING")
        self.write_requirement()

        fake_run, tracker = self._plan_then_critic([self._critic("pass")])

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_plan(self.task_id), 0)

        self.assertEqual(tracker["rounds"], 1)
        self.assertIn("Plan critic: pass", out.getvalue())
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_revise_triggers_a_replan(self):
        self.write_state(status="PLANNING")
        self.write_requirement()

        fake_run, tracker = self._plan_then_critic(
            [self._critic("revise", "high"), self._critic("pass")]
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_plan(self.task_id), 0)

        self.assertEqual(tracker["rounds"], 2)
        self.assertIn("Replanning", out.getvalue())

    def test_persistent_objection_stops_after_the_round_limit(self):
        self.write_state(status="PLANNING")
        self.write_requirement()

        fake_run, tracker = self._plan_then_critic(
            [self._critic("revise", "high")]
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_plan(self.task_id), 0)

        self.assertEqual(tracker["rounds"], orch.CRITIC_MAX_ROUNDS)
        self.assertIn("still objects", out.getvalue())
        # The developer still gets the plan, with the critique recorded.
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_low_severity_issues_do_not_bounce_the_plan(self):
        self.write_state(status="PLANNING")
        self.write_requirement()

        fake_run, tracker = self._plan_then_critic(
            [self._critic("revise", "low")]
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                self.assertEqual(orch.run_plan(self.task_id), 0)

        self.assertEqual(tracker["rounds"], 1)

    def test_unavailable_critic_does_not_block_planning(self):
        """An unavailable checker must not become a gate nobody can pass."""
        self.write_state(status="PLANNING")
        self.write_requirement()

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "codex":
                out = argv[argv.index("--output-last-message") + 1]
                orch.Path(out).write_text(plan_json())
                return mock.Mock(returncode=0, stdout="", stderr="")

            raise FileNotFoundError()

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_plan(self.task_id), 0)

        self.assertIn("did not run", out.getvalue())
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

        critiqued = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]
        self.assertFalse(critiqued[0]["ran"])

    def test_records_the_critique(self):
        self.write_state(status="PLANNING")
        self.write_requirement()

        fake_run, _ = self._plan_then_critic([self._critic("pass")])

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_plan(self.task_id)

        critiqued = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]

        self.assertTrue(critiqued[0]["ran"])
        self.assertEqual(critiqued[0]["verdict"], "pass")


class DispatchTests(unittest.TestCase):
    def test_clarify_is_dispatchable(self):
        self.assertIn("clarify", orch.ACTIONS)

    def test_usage_documents_clarify(self):
        self.assertIn("clarify", orch.USAGE)


if __name__ == "__main__":
    unittest.main()
