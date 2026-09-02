"""Phase 1 - the plan is a parsed contract, not prose that happens to parse.

Plans were emitted as YAML and never parsed: Codex's raw last message went
straight to disk, so a fenced or truncated response became an unusable plan and
nothing noticed. JSON gives a real parse with the standard library, and lets the
planner be driven by ``codex exec --output-schema``.
"""

import json
import unittest

from _support import (
    REPO_ROOT,
    TaskDirCase,
    VALID_PLAN,
    load_orchestrator,
    plan_json,
)

orch = load_orchestrator()

SCHEMA_PATH = REPO_ROOT / ".ai" / "schemas" / "plan.schema.json"


class SchemaFileTests(unittest.TestCase):
    def test_schema_exists_and_is_valid_json(self):
        schema = json.loads(SCHEMA_PATH.read_text())

        self.assertEqual(schema["type"], "object")

    def test_schema_requires_the_same_keys_the_orchestrator_does(self):
        """The schema handed to codex must match what we enforce."""
        schema = json.loads(SCHEMA_PATH.read_text())

        self.assertEqual(
            set(schema["required"]), set(orch.PLAN_REQUIRED_KEYS)
        )

    def test_acceptance_criteria_require_a_verify_field(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        criterion = schema["$defs"]["acceptanceCriterion"]

        self.assertIn("verify", criterion["required"])

    def test_fixture_plan_satisfies_our_own_validator(self):
        self.assertEqual(orch.validate_plan(VALID_PLAN), [])


class LoadPlanTests(TaskDirCase):
    def test_parses_a_valid_plan(self):
        path = self.write_plan()
        data = orch.load_plan(path)

        self.assertEqual(data["objective"], VALID_PLAN["objective"])

    def test_rejects_invalid_json_with_a_clear_message(self):
        path = self.write_plan("plan.json", "{not json")

        with self.assertRaises(RuntimeError) as ctx:
            orch.load_plan(path)

        self.assertIn("not valid JSON", str(ctx.exception))

    def test_rejects_a_fenced_response(self):
        """The exact failure the audit describes: codex wraps output in a fence."""
        fenced = "```json\n" + plan_json() + "```\n"
        path = self.write_plan("plan.json", fenced)

        with self.assertRaises(RuntimeError):
            orch.load_plan(path)

    def test_rejects_a_non_object_top_level(self):
        path = self.write_plan("plan.json", "[1, 2, 3]")

        with self.assertRaises(RuntimeError) as ctx:
            orch.load_plan(path)

        self.assertIn("must be a JSON object", str(ctx.exception))

    def test_legacy_yaml_is_refused_with_guidance(self):
        path = self.write_plan("plan.yaml", "objective: legacy\n")

        with self.assertRaises(RuntimeError) as ctx:
            orch.load_plan(path)

        message = str(ctx.exception)
        self.assertIn("not JSON", message)
        self.assertIn("re-plan", message)


class ValidatePlanTests(unittest.TestCase):
    def _plan(self, **overrides):
        data = json.loads(json.dumps(VALID_PLAN))
        data.update(overrides)
        return data

    def test_accepts_the_canonical_plan(self):
        self.assertEqual(orch.validate_plan(self._plan()), [])

    def test_reports_every_missing_key(self):
        problems = orch.validate_plan({})

        for key in orch.PLAN_REQUIRED_KEYS:
            self.assertTrue(
                any(key in p for p in problems), "no problem mentions " + key
            )

    def test_rejects_empty_objective(self):
        problems = orch.validate_plan(self._plan(objective="   "))

        self.assertTrue(any("objective" in p for p in problems))

    def test_rejects_non_string_list_entries(self):
        problems = orch.validate_plan(self._plan(requirements=["ok", 7]))

        self.assertTrue(any("requirements" in p for p in problems))

    def test_rejects_empty_acceptance_criteria(self):
        problems = orch.validate_plan(self._plan(acceptance_criteria=[]))

        self.assertTrue(any("acceptance_criteria" in p for p in problems))

    def test_requires_verify_on_every_criterion(self):
        problems = orch.validate_plan(
            self._plan(
                acceptance_criteria=[
                    {"id": "AC-1", "statement": "a thing is true"}
                ]
            )
        )

        self.assertTrue(any("verify" in p for p in problems))

    def test_rejects_duplicate_criterion_ids(self):
        problems = orch.validate_plan(
            self._plan(
                acceptance_criteria=[
                    {"id": "AC-1", "statement": "x", "verify": "true"},
                    {"id": "AC-1", "statement": "y", "verify": "true"},
                ]
            )
        )

        self.assertTrue(any("duplicate" in p for p in problems))

    def test_rejects_file_entries_without_a_path(self):
        problems = orch.validate_plan(
            self._plan(files_to_create=[{"purpose": "no path"}])
        )

        self.assertTrue(any("files_to_create" in p for p in problems))

    def test_accepts_bare_path_strings(self):
        self.assertEqual(
            orch.validate_plan(self._plan(files_to_create=["widget.py"])), []
        )


class PlanFilePathsTests(unittest.TestCase):
    def test_normalises_object_entries(self):
        data = {"files_to_create": [{"path": "a.py", "purpose": "x"}]}

        self.assertEqual(orch.plan_file_paths(data, "files_to_create"), ["a.py"])

    def test_normalises_string_entries(self):
        data = {"files_to_modify": ["b.py"]}

        self.assertEqual(orch.plan_file_paths(data, "files_to_modify"), ["b.py"])

    def test_missing_key_is_empty(self):
        self.assertEqual(orch.plan_file_paths({}, "files_to_modify"), [])


class CodexInvocationTests(unittest.TestCase):
    def test_passes_the_output_schema_when_present(self):
        argv = orch.codex_argv(REPO_ROOT, REPO_ROOT / "plan.json")

        self.assertIn("--output-schema", argv)
        self.assertIn("plan.schema.json", argv[argv.index("--output-schema") + 1])

    def test_omits_output_schema_when_absent(self):
        import tempfile
        from pathlib import Path

        empty = Path(tempfile.mkdtemp(prefix="wf-noschema-"))
        argv = orch.codex_argv(empty, empty / "plan.json")

        self.assertNotIn("--output-schema", argv)

    def test_prompt_is_read_from_stdin(self):
        """Windows caps a command line at ~8191 chars and the npm shims go
        through cmd.exe. The first real replan died in 0.33s with "The command
        line is too long" -- the failure context C7 added is what overflowed
        it, so richer context made the replan less likely to run."""
        argv = orch.codex_argv(REPO_ROOT, REPO_ROOT / "plan.json")

        self.assertEqual(argv[-1], "-")

    def test_planner_remains_sandboxed_read_only(self):
        argv = orch.codex_argv(REPO_ROOT, REPO_ROOT / "plan.json")

        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")


class PlanPromptTests(unittest.TestCase):
    def test_asks_for_json_not_yaml(self):
        prompt = orch.plan_prompt("TASK-001", "Do a thing.", replan=False)

        self.assertIn("single JSON object", prompt)
        self.assertNotIn("valid YAML", prompt)

    def test_requires_runnable_verify_commands(self):
        prompt = orch.plan_prompt("TASK-001", "Do a thing.", replan=False)

        self.assertIn("verify", prompt)
        self.assertIn("exits 0", prompt)

    def test_warns_that_file_lists_are_enforced(self):
        prompt = orch.plan_prompt("TASK-001", "Do a thing.", replan=False)

        self.assertIn("scope violation", prompt)

    def test_replan_prompt_says_so(self):
        prompt = orch.plan_prompt("TASK-001", "Do a thing.", replan=True)

        self.assertIn("previous approved plan", prompt)

    def test_requirement_is_inlined(self):
        prompt = orch.plan_prompt("TASK-001", "SENTINEL REQUIREMENT", False)

        self.assertIn("SENTINEL REQUIREMENT", prompt)


class PlanProblemsDispatchTests(TaskDirCase):
    def test_json_plan_is_parsed_and_validated(self):
        path = self.write_plan("plan.json", plan_json(objective=""))

        self.assertTrue(any("objective" in p for p in orch.plan_problems(path)))

    def test_yaml_plan_falls_back_to_structural_check(self):
        body = "".join("%s: x\n" % k for k in orch.PLAN_REQUIRED_KEYS)
        path = self.write_plan("plan.yaml", body)

        self.assertEqual(orch.plan_problems(path), [])


class PlanFilenameTests(TaskDirCase):
    def test_canonical_filename_is_json(self):
        self.assertEqual(orch.PLAN_FILENAME, "plan.json")

    def test_plan_json_wins_over_legacy_yaml_when_no_pointer(self):
        self.write_plan("plan.json")
        self.write_plan("plan.yaml", "objective: legacy\n")

        _, state = self.write_state()
        state.pop("plan_file", None)

        self.assertEqual(
            orch.resolve_plan_file(self.task_id, state).name, "plan.json"
        )


class InterpreterPlaceholderContractTests(unittest.TestCase):
    """The planner must be told how to invoke Python.

    The first real end-to-end task produced four acceptance criteria reading
    `python -m unittest ...`. All four returned 9009 -- "Python was not found"
    -- because on Windows that name is an App Execution Alias stub. The plan
    was fine; the criteria were unrunnable, so a correct implementation was
    recorded as 4 of 4 criteria failed.

    `{python}` expansion already existed in the runner. Neither the planning
    prompt nor the schema mentioned it, so the planner had no way to know: the
    contract was enforced but undocumented to the only party that could satisfy
    it.
    """

    def test_the_planning_prompt_mandates_the_placeholder(self):
        prompt = orch.plan_prompt("TASK-001", "do a thing", replan=False)

        self.assertIn("{python}", prompt)

    def test_the_planning_prompt_warns_against_a_bare_interpreter_name(self):
        prompt = orch.plan_prompt("TASK-001", "do a thing", replan=False)

        self.assertIn("9009", prompt)

    def test_the_placeholder_survives_prompt_formatting(self):
        """The prompt is an f-string, so the token needs doubled braces."""
        prompt = orch.plan_prompt("TASK-001", "do a thing", replan=False)

        self.assertNotIn("{{python}}", prompt)

    def test_the_schema_documents_the_placeholder(self):
        schema = json.loads(
            (REPO_ROOT / ".ai" / "schemas" / "plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        description = schema["$defs"]["acceptanceCriterion"]["properties"][
            "verify"
        ]["description"]

        self.assertIn("{python}", description)

    def test_the_runner_expands_the_placeholder(self):
        """Guidance is worthless if the expansion is not actually there."""
        source = (
            REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"
        ).read_text(encoding="utf-8")

        self.assertIn('verify.replace("{python}", interpreter())', source)


if __name__ == "__main__":
    unittest.main()
