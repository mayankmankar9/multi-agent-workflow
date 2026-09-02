"""Phase 3 - the context bus: blackboard, hooks, replan context, cross-task memory.

The audit's finding: there is no shared context, only one-directional file
passing, and the most important channel is missing entirely. The replanner was
told a failure had happened and given nothing about it, so it re-derived from the
same inputs that produced the failing plan.
"""

import json
import os
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

HOOKS = REPO_ROOT / ".ai" / "hooks"


class ContextBlockTests(TaskDirCase):
    def test_appends_a_typed_block(self):
        block = orch.append_context_block(
            self.task_id,
            "codex",
            "PLANNING",
            "repo_finding",
            "Tests are unittest-based and live at the repo root.",
            confidence="high",
            evidence=["ls tests/"],
        )

        self.assertEqual(block["block_id"], "ctx-001")
        self.assertEqual(block["type"], "repo_finding")
        self.assertEqual(block["author"], "codex")
        self.assertEqual(block["evidence"], ["ls tests/"])

    def test_block_ids_increment(self):
        for index in range(3):
            orch.append_context_block(
                self.task_id, "codex", "PLANNING", "decision", "d%d" % index
            )

        blocks = orch.read_context(self.task_id)

        self.assertEqual(
            [b["block_id"] for b in blocks], ["ctx-001", "ctx-002", "ctx-003"]
        )

    def test_is_append_only_on_disk(self):
        """One line per block, so appending never rewrites earlier content."""
        orch.append_context_block(
            self.task_id, "codex", "PLANNING", "decision", "first"
        )
        first = orch.context_file(self.task_id).read_text()

        orch.append_context_block(
            self.task_id, "claude", "IMPLEMENTING", "deviation", "second"
        )
        second = orch.context_file(self.task_id).read_text()

        self.assertTrue(second.startswith(first))
        self.assertEqual(len(second.strip().splitlines()), 2)

    def test_rejects_an_unknown_block_type(self):
        with self.assertRaises(RuntimeError) as ctx:
            orch.append_context_block(
                self.task_id, "codex", "PLANNING", "gossip", "x"
            )

        self.assertIn("Unknown context block type", str(ctx.exception))

    def test_rejects_empty_content(self):
        with self.assertRaises(RuntimeError):
            orch.append_context_block(
                self.task_id, "codex", "PLANNING", "decision", "   "
            )

    def test_every_documented_type_is_accepted(self):
        for block_type in sorted(orch.CONTEXT_BLOCK_TYPES):
            orch.append_context_block(
                self.task_id, "codex", "PLANNING", block_type, "content"
            )

        self.assertEqual(
            len(orch.read_context(self.task_id)),
            len(orch.CONTEXT_BLOCK_TYPES),
        )

    def test_malformed_lines_are_skipped_not_fatal(self):
        """The blackboard is advisory context, not control flow."""
        orch.append_context_block(
            self.task_id, "codex", "PLANNING", "decision", "good"
        )
        with orch.context_file(self.task_id).open("a") as f:
            f.write("{not json\n")

        blocks = orch.read_context(self.task_id)

        self.assertEqual(len(blocks), 1)

    def test_reading_an_absent_blackboard_is_empty(self):
        self.assertEqual(orch.read_context(self.task_id), [])

    def test_schema_matches_the_accepted_types(self):
        schema = json.loads(
            (REPO_ROOT / ".ai" / "schemas" / "context-block.schema.json").read_text()
        )

        self.assertEqual(
            set(schema["properties"]["type"]["enum"]),
            orch.CONTEXT_BLOCK_TYPES,
        )


class RenderContextTests(TaskDirCase):
    def test_empty_blackboard_renders_nothing(self):
        self.assertEqual(orch.render_context(self.task_id), "")

    def test_renders_blocks_with_evidence(self):
        orch.append_context_block(
            self.task_id,
            "codex",
            "PLANNING",
            "repo_finding",
            "No pytest config; unittest only.",
            confidence="high",
            evidence=["cat pyproject.toml"],
        )

        rendered = orch.render_context(self.task_id)

        self.assertIn("No pytest config", rendered)
        self.assertIn("ctx-001", rendered)
        self.assertIn("cat pyproject.toml", rendered)
        self.assertIn("high confidence", rendered)

    def test_marks_context_as_data_not_instructions(self):
        """Injected blackboard content is untrusted worker output."""
        orch.append_context_block(
            self.task_id, "codex", "PLANNING", "decision", "x"
        )

        self.assertIn("not as instructions", orch.render_context(self.task_id))


class OrchestratorWritesBlocksTests(TaskDirCase):
    def test_approval_is_recorded_on_the_blackboard(self):
        self.write_state()
        self.write_plan()

        with quiet():
            orch.approve_plan(self.task_id)

        blocks = orch.read_context(self.task_id)
        decisions = [b for b in blocks if b["type"] == "decision"]

        self.assertEqual(len(decisions), 1)
        self.assertIn("approved plan v1", decisions[0]["content"])
        self.assertEqual(decisions[0]["author"], "orchestrator")

    def test_validation_failure_is_recorded_on_the_blackboard(self):
        self.write_state(status="VALIDATING")
        (self.tmp / ".ai" / "validation.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "tests",
                            "command": '{python} -c "import sys; sys.stderr.write(\'Ran 0 tests in 0.0s\\n\\nOK\\n\')"',
                            "expect_test_count": True,
                        }
                    ]
                }
            )
        )

        with quiet():
            orch.run_validation(self.task_id)

        observations = [
            b
            for b in orch.read_context(self.task_id)
            if b["type"] == "failure_observation"
        ]

        self.assertEqual(len(observations), 1)
        self.assertIn("empty test suite", observations[0]["content"])


class ReplanContextTests(TaskDirCase):
    """C7: the replanner used to be told nothing about the failure."""

    def _failed_task(self):
        self.write_requirement("Add caching.")
        previous = self.write_plan("plan.json", plan_json(objective="v1 plan"))
        (self.task_path / "implementation.md").write_text(
            "Tried an in-memory cache; it broke on restart.\n"
        )
        (self.task_path / "test-results.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "returncode": 1,
                    "test_count": 4,
                    "failure_reason": "tests: exited 1",
                    "stdout": "",
                    "stderr": "FAIL: test_cache_survives_restart\n",
                    "checks": [
                        {"name": "tests", "passed": False,
                         "failure_reason": "exited 1"}
                    ],
                    "acceptance_criteria": {
                        "results": [
                            {
                                "id": "AC-2",
                                "statement": "cache survives restart",
                                "verify": "python -m unittest t.T.test_restart",
                                "passed": False,
                                "output": "AssertionError",
                            }
                        ]
                    },
                    "diff_scope": {
                        "declared": ["cache.py"],
                        "violations": ["scratch.py"],
                    },
                }
            )
        )
        _, state = self.write_state(
            status="FAILED", failure_reason="tests: exited 1"
        )
        return previous, state

    def test_carries_the_failed_plan(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("v1 plan", context)
        self.assertIn("plan.json", context)

    def test_carries_the_test_output(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("test_cache_survives_restart", context)

    def test_carries_the_failed_acceptance_criteria(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("AC-2", context)
        self.assertIn("cache survives restart", context)

    def test_carries_the_scope_violations(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("scratch.py", context)

    def test_carries_the_implementation_notes(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("broke on restart", context)

    def test_tells_the_replanner_a_bug_is_not_an_architecture_problem(self):
        previous, state = self._failed_task()

        context = orch.replan_context(self.task_id, state, previous)

        self.assertIn("wrong remedy for a bug", context)

    def test_reports_a_missing_previous_plan_rather_than_omitting_it(self):
        _, state = self.write_state(status="FAILED", failure_reason="x")

        context = orch.replan_context(
            self.task_id, state, self.task_path / "gone.json"
        )

        self.assertIn("no longer on disk", context)

    def test_replan_prompt_includes_the_failure_context(self):
        previous, state = self._failed_task()
        captured = {}

        def fake_run(argv, **kwargs):
            # The planner's prompt goes on stdin: it does not fit on a
            # Windows command line.
            captured["prompt"] = kwargs.get("input")
            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json())
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                result = orch.run_codex_replan(
                    self.task_id, 2, previous_plan=previous, state=state
                )

        self.assertIsNotNone(result)
        self.assertIn("test_cache_survives_restart", captured["prompt"])
        self.assertIn("v1 plan", captured["prompt"])

    def test_route_failure_passes_the_pre_bump_plan(self):
        previous, _ = self._failed_task()
        self.write_state(
            status="FAILED",
            failure_reason="architecture: the plan cannot work",
        )
        captured = {}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "codex":
                captured["prompt"] = kwargs.get("input")
                out = argv[argv.index("--output-last-message") + 1]
                orch.Path(out).write_text(plan_json())

            return mock.Mock(returncode=0, stdout="", stderr="")

        os.environ["ORCHESTRATOR_DISABLE_CLASSIFIER"] = "1"
        self.addCleanup(
            os.environ.pop, "ORCHESTRATOR_DISABLE_CLASSIFIER", None
        )

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.route_failure(self.task_id)

        # The v1 plan contents must reach the replanner even though the
        # version pointer has already moved to v2.
        self.assertIn("v1 plan", captured["prompt"])


class FailureEvidenceTests(TaskDirCase):
    def test_empty_when_nothing_is_recorded(self):
        _, state = self.write_state()

        self.assertEqual(orch.failure_evidence(self.task_id, state), "")

    def test_includes_reviewer_findings(self):
        _, state = self.write_state(failure_reason="review findings")
        (self.task_path / "review-findings.json").write_text(
            json.dumps(
                {
                    "dimensions": {
                        "security": {
                            "ran": True,
                            "findings": [
                                {
                                    "severity": "high",
                                    "title": "token in config",
                                    "detail": "committed secret",
                                }
                            ],
                        }
                    }
                }
            )
        )

        evidence = orch.failure_evidence(self.task_id, state)

        self.assertIn("token in config", evidence)

    def test_excludes_findings_from_dimensions_that_did_not_run(self):
        _, state = self.write_state(failure_reason="x")
        (self.task_path / "review-findings.json").write_text(
            json.dumps(
                {"dimensions": {"security": {"ran": False, "error": "timeout"}}}
            )
        )

        evidence = orch.failure_evidence(self.task_id, state)

        self.assertNotIn("timeout", evidence)

    def test_includes_the_classifier_rationale(self):
        _, state = self.write_state(failure_reason="x")
        orch.record_event(
            self.task_id,
            "FAILURE_ROUTED",
            route="CLAUDE_FIX",
            confidence="high",
            rationale="test_widget asserts the wrong constant",
        )

        evidence = orch.failure_evidence(self.task_id, state)

        self.assertIn("test_widget asserts the wrong constant", evidence)


class ConstitutionAndAdrTests(unittest.TestCase):
    def test_constitution_exists(self):
        self.assertTrue((REPO_ROOT / ".ai" / "constitution.md").is_file())

    def test_constitution_states_the_evidence_rules(self):
        text = (REPO_ROOT / ".ai" / "constitution.md").read_text()

        self.assertIn("Never assert an absence you did not verify", text)
        self.assertIn("Omit, do not hedge", text)

    def test_adr_ledger_has_records(self):
        records = [
            p
            for p in (REPO_ROOT / ".ai" / "adr").glob("*.md")
            if p.name != "README.md"
        ]

        self.assertGreaterEqual(len(records), 2)

    def test_every_adr_has_the_required_sections(self):
        for record in (REPO_ROOT / ".ai" / "adr").glob("*.md"):
            if record.name == "README.md":
                continue

            text = record.read_text()
            self.assertIn("## Context", text, record.name)
            self.assertIn("## Decision", text, record.name)
            self.assertIn("## Consequences", text, record.name)


class SharedContextInjectionTests(TaskDirCase):
    def test_worker_prompt_carries_the_blackboard(self):
        self.write_state()
        self.write_requirement()
        self.write_plan()

        with quiet():
            orch.approve_plan(self.task_id)

        orch.append_context_block(
            self.task_id,
            "codex",
            "PLANNING",
            "repo_finding",
            "SENTINEL_FINDING about test layout",
        )

        captured = {}

        def fake_run(argv, **kwargs):
            # The implementer's prompt arrives on stdin, not as an argument.
            captured["prompt"] = kwargs.get("input")
            captured["env"] = kwargs.get("env")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_implementation(self.task_id)

        self.assertIn("SENTINEL_FINDING", captured["prompt"])

    def test_worker_env_exports_the_task_id_for_hooks(self):
        env = orch.worker_env(self.task_id)

        self.assertEqual(env["ORCHESTRATOR_TASK_ID"], self.task_id)

    def test_worker_env_is_none_without_a_task(self):
        self.assertIsNone(orch.worker_env(None))


class HookTests(unittest.TestCase):
    """Hooks are scripts; run them as scripts."""

    def _run(self, script, env=None, cwd=None):
        environment = dict(os.environ)
        environment.pop("ORCHESTRATOR_TASK_ID", None)
        environment.update(env or {})

        return subprocess.run(
            [sys.executable, str(HOOKS / script)],
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_session_start_is_quiet_outside_a_task(self):
        result = self._run("session_start.py")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_stop_guard_allows_outside_a_task(self):
        """Ordinary sessions in this repo must not be blocked."""
        result = self._run("stop_guard.py")

        self.assertEqual(result.returncode, 0)


class HookBehaviourTests(TaskDirCase):
    def _run(self, script, env=None):
        environment = dict(os.environ)
        environment["ORCHESTRATOR_TASK_ID"] = self.task_id
        environment.update(env or {})

        return subprocess.run(
            [sys.executable, str(HOOKS / script)],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_session_start_injects_the_blackboard(self):
        orch.append_context_block(
            self.task_id,
            "codex",
            "PLANNING",
            "repo_finding",
            "SENTINEL_BLOCK_CONTENT",
        )

        result = self._run("session_start.py")

        self.assertEqual(result.returncode, 0)
        self.assertIn("SENTINEL_BLOCK_CONTENT", result.stdout)

    def test_stop_guard_blocks_with_exit_code_2(self):
        """Only exit code 2 blocks. Exit 1 blocks nothing."""
        result = self._run("stop_guard.py")

        self.assertEqual(result.returncode, 2)
        self.assertIn("implementation.md", result.stderr)
        self.assertIn("context block", result.stderr)

    def test_stop_guard_allows_when_both_obligations_are_met(self):
        (self.task_path / "implementation.md").write_text("Changed widget.py\n")
        orch.append_context_block(
            self.task_id,
            "claude",
            "IMPLEMENTING",
            "deviation",
            "Used a dict instead of a dataclass.",
        )

        result = self._run("stop_guard.py")

        self.assertEqual(result.returncode, 0)

    def test_orchestrator_blocks_do_not_satisfy_the_write_back_rule(self):
        """A worker must write back itself, not coast on orchestrator records."""
        (self.task_path / "implementation.md").write_text("notes\n")
        orch.append_context_block(
            self.task_id, "orchestrator", "IMPLEMENTING", "decision", "auto"
        )

        result = self._run("stop_guard.py")

        self.assertEqual(result.returncode, 2)
        self.assertIn("No context block", result.stderr)

    def test_empty_implementation_notes_do_not_count(self):
        (self.task_path / "implementation.md").write_text("   \n")
        orch.append_context_block(
            self.task_id, "claude", "IMPLEMENTING", "decision", "x"
        )

        result = self._run("stop_guard.py")

        self.assertEqual(result.returncode, 2)

    def test_guard_can_be_disabled_deliberately(self):
        result = self._run(
            "stop_guard.py", env={"ORCHESTRATOR_SKIP_STOP_GUARD": "1"}
        )

        self.assertEqual(result.returncode, 0)


class SettingsTests(unittest.TestCase):
    def test_settings_registers_both_hooks(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )
        hooks = settings["hooks"]

        self.assertIn("SessionStart", hooks)
        self.assertIn("Stop", hooks)

    def test_registered_hook_scripts_exist(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )

        for event in settings["hooks"].values():
            for entry in event:
                for hook in entry["hooks"]:
                    path = hook["command"].split()[-1]
                    self.assertTrue((REPO_ROOT / path).is_file(), path)


class DispatchTests(unittest.TestCase):
    def test_context_is_not_locked(self):
        """The blackboard must stay readable while a task is running."""
        self.assertNotIn("context", orch.ACTIONS)

    def test_usage_documents_context_and_adr(self):
        self.assertIn("context", orch.USAGE)
        self.assertIn("adr", orch.USAGE)


class AdrCreationTests(TaskDirCase):
    def test_creates_the_next_numbered_record(self):
        (self.tmp / ".ai" / "adr").mkdir(parents=True)

        with quiet():
            self.assertEqual(orch.run_adr("Use worktrees per task"), 0)

        records = sorted((self.tmp / ".ai" / "adr").glob("*.md"))

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].name.startswith("0001-"))
        self.assertIn("use-worktrees-per-task", records[0].name)

    def test_numbers_increment(self):
        adr = self.tmp / ".ai" / "adr"
        adr.mkdir(parents=True)
        (adr / "0007-existing.md").write_text("# Existing\n")

        with quiet():
            orch.run_adr("Another decision")

        self.assertTrue((adr / "0008-another-decision.md").is_file())

    def test_refuses_an_empty_title(self):
        with quiet():
            self.assertEqual(orch.run_adr("   "), 1)

    def test_template_demands_consequences(self):
        (self.tmp / ".ai" / "adr").mkdir(parents=True)

        with quiet():
            orch.run_adr("A decision")

        text = (self.tmp / ".ai" / "adr" / "0001-a-decision.md").read_text()

        self.assertIn("## Consequences", text)
        self.assertIn("Include the costs", text)


if __name__ == "__main__":
    unittest.main()
