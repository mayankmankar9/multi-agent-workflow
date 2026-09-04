"""Phase 4 - hooks as controls, sandbox boundary, cost accounting.

Everything CLAUDE.md previously *asked* for -- do not write state.json, do not
commit, do not push -- was prompt text, which is a request. This phase makes it
a control, and starts recording what a task actually cost.
"""

import importlib.util
import json
import os
import re
import shutil
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

DENY = 2
ALLOW = 0


def run_hook(script, payload, cwd=None, task_id="TASK-001"):
    """Run a hook against a payload.

    Defaults to a *worker* session. ``pre_tool_use`` enforces only when
    ORCHESTRATOR_TASK_ID is set, because the thing it guards against is a
    worker forging evidence, and that variable is exported by ``worker_env()``
    and by nothing else. Pass ``task_id=None`` for a developer-driven session.

    Setting it explicitly rather than inheriting matters: these tests would
    otherwise pass or fail depending on whether the suite happened to be run
    from inside a worker, which is the sort of ambient dependency that makes a
    guard look tested when it is not.
    """
    environment = dict(os.environ)

    if task_id:
        environment["ORCHESTRATOR_TASK_ID"] = task_id
    else:
        environment.pop("ORCHESTRATOR_TASK_ID", None)

    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=environment,
    )


class PreToolUseWriteTests(unittest.TestCase):
    """Orchestrator-owned artifacts must be undeniably undeniable."""

    def _write(self, path, tool="Write"):
        return run_hook(
            "pre_tool_use.py",
            {"tool_name": tool, "tool_input": {"file_path": path}},
        )

    def test_denies_writing_state_json(self):
        result = self._write(".ai/tasks/TASK-001/state.json")

        self.assertEqual(result.returncode, DENY)
        self.assertIn("forging evidence", result.stderr)

    def test_denies_writing_test_results(self):
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/test-results.json").returncode, DENY
        )

    def test_denies_writing_events_log(self):
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/events.jsonl").returncode, DENY
        )

    def test_denies_writing_validation_record(self):
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/validation.md").returncode, DENY
        )

    def test_denies_writing_reviewer_findings(self):
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/review-findings.json").returncode,
            DENY,
        )

    def test_denies_editing_the_orchestrator(self):
        self.assertEqual(
            self._write(".ai/scripts/orchestrator.py", tool="Edit").returncode,
            DENY,
        )

    def test_denies_editing_the_hooks_themselves(self):
        self.assertEqual(
            self._write(".ai/hooks/stop_guard.py", tool="Edit").returncode, DENY
        )

    def test_denies_editing_settings(self):
        self.assertEqual(
            self._write(".claude/settings.json", tool="Edit").returncode, DENY
        )

    def test_allows_ordinary_source_files(self):
        self.assertEqual(self._write("widget.py").returncode, ALLOW)

    def test_allows_implementation_notes(self):
        """The worker is required to write these."""
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/implementation.md").returncode,
            ALLOW,
        )

    def test_allows_the_context_blackboard(self):
        """Write-back is mandatory, so it must not be denied."""
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/context.jsonl").returncode, ALLOW
        )

    def test_denies_windows_style_separators(self):
        result = self._write(".ai\\tasks\\TASK-001\\state.json")

        self.assertEqual(result.returncode, DENY)

    def test_denies_a_path_with_a_leading_dot_slash(self):
        self.assertEqual(
            self._write("./.ai/tasks/TASK-001/state.json").returncode, DENY
        )


class PreToolUseBashTests(unittest.TestCase):
    def _bash(self, command):
        return run_hook(
            "pre_tool_use.py",
            {"tool_name": "Bash", "tool_input": {"command": command}},
        )

    def test_denies_git_commit(self):
        result = self._bash("git commit -m 'work'")

        self.assertEqual(result.returncode, DENY)
        self.assertIn("developer commits", result.stderr)

    def test_denies_git_push(self):
        self.assertEqual(self._bash("git push origin main").returncode, DENY)

    def test_denies_history_rewrites(self):
        for command in ("git reset --hard HEAD~1", "git rebase main"):
            self.assertEqual(self._bash(command).returncode, DENY, command)

    def test_denies_commit_hidden_behind_a_chain(self):
        self.assertEqual(
            self._bash("cd . && git commit -am wip").returncode, DENY
        )

    def test_denies_redirection_into_a_protected_path(self):
        """The heredoc loophole the whole tool boundary exists to close."""
        result = self._bash("echo '{}' > .ai/tasks/TASK-001/state.json")

        self.assertEqual(result.returncode, DENY)
        self.assertIn("bypasses the tool-level permission surface", result.stderr)

    def test_denies_append_redirection_into_a_protected_path(self):
        self.assertEqual(
            self._bash("echo x >> .ai/tasks/TASK-001/events.jsonl").returncode,
            DENY,
        )

    def test_allows_reading_a_protected_path(self):
        self.assertEqual(
            self._bash("cat .ai/tasks/TASK-001/state.json").returncode, ALLOW
        )

    def test_allows_git_status_and_diff(self):
        for command in ("git status", "git diff HEAD", "git log --oneline"):
            self.assertEqual(self._bash(command).returncode, ALLOW, command)

    def test_allows_running_tests(self):
        self.assertEqual(self._bash("python -m unittest -v").returncode, ALLOW)

    def test_allows_redirection_to_an_ordinary_file(self):
        self.assertEqual(self._bash("echo hi > notes.txt").returncode, ALLOW)


class PreToolUseRobustnessTests(unittest.TestCase):
    """A hook that fails closed on its own input format bricks the repo."""

    def test_empty_stdin_allows(self):
        result = subprocess.run(
            [sys.executable, str(HOOKS / "pre_tool_use.py")],
            input="",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, ALLOW)

    def test_malformed_json_allows(self):
        result = subprocess.run(
            [sys.executable, str(HOOKS / "pre_tool_use.py")],
            input="{not json",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, ALLOW)

    def test_unknown_payload_shape_allows(self):
        self.assertEqual(
            run_hook("pre_tool_use.py", {"something": "else"}).returncode, ALLOW
        )

    def test_unknown_tool_allows(self):
        self.assertEqual(
            run_hook(
                "pre_tool_use.py",
                {"tool_name": "WebFetch", "tool_input": {"url": "x"}},
            ).returncode,
            ALLOW,
        )

    def test_never_uses_exit_code_one_to_deny(self):
        """Exit 1 blocks nothing; using it for a refusal is a silent failure."""
        source = (HOOKS / "pre_tool_use.py").read_text()

        self.assertIn("DENY = 2", source)
        self.assertNotIn("return 1", source)


class PreToolUseSessionScopeTests(unittest.TestCase):
    """The guard binds workers, not the developer.

    This scoping was not a relaxation of a working control -- the hook had been
    registered as ``python .ai/hooks/pre_tool_use.py``, which on Windows
    resolves to an App Execution Alias stub that exits 9009, and on most Linux
    images does not resolve at all. It denied nothing, in either place, for as
    long as it existed. The first session in which it genuinely fired was one
    editing orchestrator.py deliberately, which is how this repo changes.

    So the denials now key on ORCHESTRATOR_TASK_ID, the same signal
    session_start and stop_guard already gate on. A worker cannot clear it: the
    orchestrator sets it in the environment it hands the child.
    """

    def _write(self, path, task_id):
        return run_hook(
            "pre_tool_use.py",
            {"tool_name": "Edit", "tool_input": {"file_path": path}},
            task_id=task_id,
        )

    def _bash(self, command, task_id):
        return run_hook(
            "pre_tool_use.py",
            {"tool_name": "Bash", "tool_input": {"command": command}},
            task_id=task_id,
        )

    def test_worker_cannot_edit_the_orchestrator(self):
        result = self._write(".ai/scripts/orchestrator.py", "TASK-001")

        self.assertEqual(result.returncode, DENY)

    def test_developer_can_edit_the_orchestrator(self):
        result = self._write(".ai/scripts/orchestrator.py", None)

        self.assertEqual(result.returncode, ALLOW)

    def test_worker_cannot_edit_the_guard_itself(self):
        self.assertEqual(
            self._write(".ai/hooks/pre_tool_use.py", "TASK-001").returncode,
            DENY,
        )

    def test_developer_can_edit_the_guard_itself(self):
        """Otherwise the guard is unfixable without bypassing it."""
        self.assertEqual(
            self._write(".ai/hooks/pre_tool_use.py", None).returncode, ALLOW
        )

    def test_worker_cannot_forge_state(self):
        self.assertEqual(
            self._write(".ai/tasks/TASK-001/state.json", "TASK-001").returncode,
            DENY,
        )

    def test_worker_cannot_commit(self):
        self.assertEqual(
            self._bash("git commit -m 'work'", "TASK-001").returncode, DENY
        )

    def test_developer_can_commit(self):
        """The developer commits; CLAUDE.md still says agents ask first."""
        self.assertEqual(
            self._bash("git commit -m 'work'", None).returncode, ALLOW
        )

    def test_a_blank_task_id_is_a_developer_session(self):
        """An empty string must not read as 'inside a task'."""
        result = run_hook(
            "pre_tool_use.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": ".ai/scripts/orchestrator.py"},
            },
            task_id="   ",
        )

        self.assertEqual(result.returncode, ALLOW)


class PostToolUseTests(TaskDirCase):
    def test_reports_a_syntax_error(self):
        bad = self.tmp / "broken.py"
        bad.write_text("def f(:\n")

        result = run_hook(
            "post_tool_use.py",
            {"tool_name": "Edit", "tool_input": {"file_path": str(bad)}},
            cwd=self.tmp,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("does not compile", result.stderr)

    def test_silent_on_valid_python(self):
        good = self.tmp / "fine.py"
        good.write_text("def f():\n    return 1\n")

        result = run_hook(
            "post_tool_use.py",
            {"tool_name": "Edit", "tool_input": {"file_path": str(good)}},
            cwd=self.tmp,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "")

    def test_ignores_non_python_files(self):
        other = self.tmp / "notes.md"
        other.write_text("# not python\n")

        result = run_hook(
            "post_tool_use.py",
            {"tool_name": "Edit", "tool_input": {"file_path": str(other)}},
            cwd=self.tmp,
        )

        self.assertEqual(result.returncode, 0)

    def test_is_advisory_and_never_denies(self):
        """A lint disagreement must not deny a call that already succeeded."""
        source = (HOOKS / "post_tool_use.py").read_text()

        self.assertNotIn("return 2", source)


class SettingsTests(unittest.TestCase):
    def test_all_four_lifecycle_events_are_registered(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )

        for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
            self.assertIn(event, settings["hooks"], event)

    def test_pre_tool_use_matches_write_tools_and_bash(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )
        matcher = settings["hooks"]["PreToolUse"][0]["matcher"]

        for tool in ("Edit", "Write", "Bash"):
            self.assertIn(tool, matcher)

    def test_every_registered_script_exists(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )

        for event in settings["hooks"].values():
            for entry in event:
                for hook in entry["hooks"]:
                    path = hook["command"].split()[-1]
                    self.assertTrue((REPO_ROOT / path).is_file(), path)


class SandboxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.pop("ORCHESTRATOR_IMPLEMENTER_WRAPPER", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("ORCHESTRATOR_IMPLEMENTER_WRAPPER", None)

        if self._prev is not None:
            os.environ["ORCHESTRATOR_IMPLEMENTER_WRAPPER"] = self._prev

    def test_no_wrapper_by_default(self):
        argv = orch.implementer_argv()

        self.assertEqual(worker_name(argv), "claude")

    def test_wrapper_prefixes_the_invocation(self):
        os.environ["ORCHESTRATOR_IMPLEMENTER_WRAPPER"] = (
            "docker run --rm -v /w:/w -w /w img"
        )
        argv = orch.implementer_argv()

        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertIn("claude", argv)

    def test_wrapper_uses_the_image_claude_not_the_host_path(self):
        """A host-resolved .CMD path does not exist inside the container."""
        os.environ["ORCHESTRATOR_IMPLEMENTER_WRAPPER"] = "docker run img"
        argv = orch.implementer_argv()

        self.assertEqual(argv[argv.index("img") + 1], "claude")

    def test_the_prompt_is_not_an_argument(self):
        """It goes on stdin: --allowedTools is variadic and would eat it."""
        argv = orch.implementer_argv()

        self.assertEqual(argv[-1], orch.CLAUDE_ALLOWED_TOOLS)

    def test_wrapper_preserves_the_tool_allowlist(self):
        os.environ["ORCHESTRATOR_IMPLEMENTER_WRAPPER"] = "sandbox-exec"
        argv = orch.implementer_argv()

        self.assertIn("--allowedTools", argv)
        self.assertEqual(
            argv[argv.index("--permission-mode") + 1], "dontAsk"
        )

    def test_blank_wrapper_is_ignored(self):
        os.environ["ORCHESTRATOR_IMPLEMENTER_WRAPPER"] = "   "

        self.assertEqual(worker_name(orch.implementer_argv()), "claude")


class WorkerUsageTests(TaskDirCase):
    def test_records_duration_for_a_successful_run(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["codex", "exec"], 10, task_id=self.task_id)

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["worker"], "codex")
        self.assertIsNotNone(usage[0]["duration_s"])

    def test_missing_cost_is_null_not_zero(self):
        """Unknown is not free."""

        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude"], 10, task_id=self.task_id)

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertIsNone(usage[0]["cost_usd"])
        self.assertIsNone(usage[0]["input_tokens"])

    def test_records_usage_for_a_failed_run(self):
        """A run that burned time and then failed belongs in the ledger."""

        def fake_run(argv, **kwargs):
            raise orch.subprocess.CalledProcessError(1, argv)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(orch.subprocess.CalledProcessError):
                orch.run_worker(["claude"], 10, task_id=self.task_id)

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertEqual(len(usage), 1)

    def test_no_usage_recorded_without_a_task(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["codex"], 10)

        self.assertEqual(self.events(), [])

    def test_parses_reported_cost_and_tokens(self):
        usage = orch.parse_worker_usage(
            json.dumps(
                {
                    "total_cost_usd": 0.0123,
                    "usage": {"input_tokens": 1500, "output_tokens": 320},
                }
            )
        )

        self.assertEqual(usage["cost_usd"], 0.0123)
        self.assertEqual(usage["input_tokens"], 1500)
        self.assertEqual(usage["output_tokens"], 320)

    def test_parses_prompt_completion_token_names(self):
        usage = orch.parse_worker_usage(
            json.dumps({"prompt_tokens": 10, "completion_tokens": 20})
        )

        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 20)

    def test_absent_usage_is_none(self):
        self.assertIsNone(orch.parse_worker_usage("no json here"))
        self.assertIsNone(orch.parse_worker_usage(""))
        self.assertIsNone(orch.parse_worker_usage('{"unrelated": 1}'))


class CostReportTests(TaskDirCase):
    def test_reports_nothing_when_no_usage_recorded(self):
        with quiet() as out:
            self.assertEqual(orch.show_cost(self.task_id), 0)

        self.assertIn("No worker usage recorded", out.getvalue())

    def test_totals_known_costs(self):
        orch.record_event(
            self.task_id,
            "WORKER_USAGE",
            worker="codex",
            duration_s=12.5,
            cost_usd=0.02,
            input_tokens=100,
            output_tokens=50,
        )
        orch.record_event(
            self.task_id,
            "WORKER_USAGE",
            worker="claude",
            duration_s=30.0,
            cost_usd=0.05,
            input_tokens=200,
            output_tokens=80,
        )

        with quiet() as out:
            orch.show_cost(self.task_id)

        text = out.getvalue()

        self.assertIn("TOTAL", text)
        self.assertIn("0.0700", text)
        self.assertIn("42.50", text)

    def test_unknown_cost_reported_as_unknown_not_zero(self):
        orch.record_event(
            self.task_id,
            "WORKER_USAGE",
            worker="claude",
            duration_s=5.0,
            cost_usd=None,
            input_tokens=None,
            output_tokens=None,
        )

        with quiet() as out:
            orch.show_cost(self.task_id)

        text = out.getvalue()

        self.assertIn("unknown", text)
        self.assertIn("Unknown, not zero", text)


def load_guard():
    """Import the hook as a module, to read the patterns it enforces with.

    The hook is a script; importing it is only for reading its constants, and
    every behavioural assertion below still runs it as a subprocess.
    """
    spec = importlib.util.spec_from_file_location(
        "workflow_pre_tool_use", HOOKS / "pre_tool_use.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def documented_evidence_paths():
    """The orchestrator-owned artifact list, parsed out of CLAUDE.md.

    Parsed rather than grepped: what has to agree with the guard is the list
    the developer is shown, so the list is what gets read.
    """
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    heading = "## Orchestrator-owned artifacts"
    start = text.find(heading)

    if start < 0:
        raise AssertionError("CLAUDE.md has no %r section" % heading)

    section = text[start + len(heading):]
    end = section.find("\n## ")
    section = section if end < 0 else section[:end]
    found = []

    for line in section.splitlines():
        match = re.match(r"^- `([^`]+)`\s*$", line.strip())

        if match:
            found.append(match.group(1))

    if not found:
        raise AssertionError("CLAUDE.md lists no orchestrator-owned artifacts")

    return found


def documented_pattern(documented):
    """A documented path convention, as a matcher.

    ``*`` is a path segment, ``<placeholder>`` is whatever the placeholder
    names -- a round is digits, anything else is a segment. Translating the
    documentation into a matcher is what makes "the same convention" a
    checkable claim rather than two texts that happen to look alike.
    """
    out = []

    for token in re.split(r"(\*|<[^>]+>)", documented):
        if token == "*":
            out.append(r"[^/]+")
        elif token.startswith("<") and token.endswith(">"):
            out.append(r"[0-9]+" if "round" in token else r"[^/]+")
        else:
            out.append(re.escape(token))

    return re.compile("^" + "".join(out) + "$")


class ProtectedCritiqueEvidenceTests(TaskDirCase):
    """A persisted critique is a read-only checker's verdict on a plan.

    Calling it orchestrator-owned in a plan constraint while the guard did not
    list it would ship a new evidence file a worker can write its own version
    of -- against the rule that no plan can authorise a worker to write its own
    verdict.
    """

    CRITIQUE = "critique-plan-round-1.json"

    def _approved_plan_declaring(self, *paths):
        self.write_requirement()
        self.write_plan(
            "plan.json",
            plan_json(
                files_to_create=[
                    {"path": path, "purpose": "declared"} for path in paths
                ]
            ),
        )
        self.write_state(status="AWAITING_APPROVAL")

        with quiet():
            orch.approve_plan(self.task_id)

    def _write(self, path):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ORCHESTRATOR_")
        }
        environment["ORCHESTRATOR_TASK_ID"] = self.task_id

        return subprocess.run(
            [sys.executable, str(HOOKS / "pre_tool_use.py")],
            input=json.dumps(
                {"tool_name": "Write", "tool_input": {"file_path": path}}
            ),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_worker_write_to_critique_artifact_is_denied(self):
        artifact = ".ai/tasks/%s/%s" % (self.task_id, self.CRITIQUE)
        # Declared in the plan the developer approved, which is the case that
        # has to be denied: the tier exists because no plan can authorise it.
        self._approved_plan_declaring(artifact, ".ai/schemas/new.schema.json")

        denied = self._write(artifact)

        self.assertEqual(denied.returncode, DENY)
        self.assertIn("orchestrator-owned", denied.stderr)

        # The plan really is being read -- an infrastructure file declared in
        # the same plan is allowed -- so the denial above is about the tier and
        # not about a plan the hook failed to find.
        allowed = self._write(".ai/schemas/new.schema.json")

        self.assertEqual(allowed.returncode, ALLOW, allowed.stderr)

    def test_the_whole_critique_naming_family_is_denied(self):
        """A guard matching one example would leave the real artifact open.

        The file the developer reads at the approval gate is the last round of
        the current plan version, so a pattern pinned to
        ``critique-plan-round-1.json`` would protect the one nobody reads.
        """
        self._approved_plan_declaring("widget.py")

        for name in (
            "critique-plan-round-1.json",
            "critique-plan-round-2.json",
            "critique-plan-v4-round-2.json",
            "critique-plan-v12-round-11.json",
        ):
            with self.subTest(name):
                result = self._write(
                    ".ai/tasks/%s/%s" % (self.task_id, name)
                )

                self.assertEqual(result.returncode, DENY)
                self.assertIn("orchestrator-owned", result.stderr)

    def test_an_absolute_path_to_a_critique_is_denied(self):
        self._approved_plan_declaring("widget.py")
        target = self.task_path.resolve() / self.CRITIQUE

        result = self._write(str(target))

        self.assertEqual(result.returncode, DENY)

    def test_documented_critique_pattern_matches_guard_contract(self):
        """CLAUDE.md's convention and the executed guard, compared on paths."""
        documented = documented_evidence_paths()
        guard = load_guard()

        # Every artifact CLAUDE.md calls orchestrator-owned is denied, using
        # the documented convention to build the path.
        self._approved_plan_declaring("widget.py")

        for entry in documented:
            concrete = (
                entry.replace("*", self.task_id)
                .replace("<plan-stem>", "plan-v4")
                .replace("<round>", "2")
            )

            with self.subTest(entry):
                self.assertNotIn("<", concrete)
                result = self._write(concrete)

                self.assertEqual(result.returncode, DENY, concrete)
                self.assertIn("orchestrator-owned", result.stderr)

        # And the critique convention CLAUDE.md documents decides exactly the
        # same paths as the pattern the guard enforces -- neither wider nor
        # narrower, so a stale round and a plain filename land the same way in
        # both.
        critique_doc = [
            entry for entry in documented if "critique-" in entry
        ]

        self.assertEqual(len(critique_doc), 1, critique_doc)

        matcher = documented_pattern(critique_doc[0])
        guard_patterns = [
            pattern
            for pattern in guard.PROTECTED_EVIDENCE_PATTERNS
            if "critique" in pattern
        ]

        self.assertEqual(len(guard_patterns), 1, guard_patterns)
        self.assertEqual(len(documented), len(guard.PROTECTED_EVIDENCE_PATTERNS))

        samples = [
            ".ai/tasks/TASK-042/critique-plan-round-1.json",
            ".ai/tasks/TASK-042/critique-plan-v4-round-2.json",
            ".ai/tasks/TASK-042/critique-plan-v4-round-12.json",
            ".ai/tasks/TASK-042/critique-plan-round-one.json",
            ".ai/tasks/TASK-042/critique-plan-v4-round-2.md",
            ".ai/tasks/TASK-042/critique.json",
            ".ai/tasks/TASK-042/notes/critique-plan-round-1.json",
            "critique-plan-round-1.json",
        ]

        for sample in samples:
            with self.subTest(sample):
                self.assertEqual(
                    bool(matcher.match(sample)),
                    bool(re.search(guard_patterns[0], sample)),
                    sample,
                )


class WorktreePreToolUseTests(TaskDirCase):
    """The guard runs from the worktree and authorises from the checkout.

    Once a worker's cwd is its recorded worktree, the copy of this hook that
    executes is that worktree's. The approved plan it must authorise against is
    the checkout's: a worktree copy of the plan would authorise its own bytes.
    """

    def setUp(self):
        super().setUp()

        self.worktree = self.tmp / ".worktrees" / self.task_id
        (self.worktree / ".ai" / "hooks").mkdir(parents=True)
        shutil.copy2(
            HOOKS / "pre_tool_use.py",
            self.worktree / ".ai" / "hooks" / "pre_tool_use.py",
        )

        self.write_requirement()
        self.write_plan(
            "plan.json",
            plan_json(
                files_to_modify=[
                    {"path": ".ai/scripts/orchestrator.py", "purpose": "declared"}
                ]
            ),
        )
        self.write_state(
            status="AWAITING_APPROVAL",
            worktree=".worktrees/%s" % self.task_id,
        )

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_state(
            status="IMPLEMENTING", worktree=".worktrees/%s" % self.task_id
        )

        # A decoy the worktree copy must not believe: same task id, same file
        # names, a plan declaring something the developer never approved.
        decoy = self.worktree / ".ai" / "tasks" / self.task_id
        decoy.mkdir(parents=True)
        shutil.copy2(self.task_path / "state.json", decoy / "state.json")
        shutil.copy2(self.task_path / "events.jsonl", decoy / "events.jsonl")
        (decoy / "plan.json").write_text(
            plan_json(
                files_to_modify=[
                    {"path": ".ai/hooks/pre_tool_use.py", "purpose": "decoy"}
                ]
            ),
            encoding="utf-8",
        )

    def _write(self, path):
        """Run the worktree's copy, from the worktree, as the harness would."""
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ORCHESTRATOR_")
        }
        environment.update(orch.worker_env(self.task_id))

        return subprocess.run(
            [
                sys.executable,
                str(self.worktree / ".ai" / "hooks" / "pre_tool_use.py"),
            ],
            input=json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": path}}
            ),
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_authoritative_plan_allows_only_declared_infrastructure(self):
        declared = self._write(".ai/scripts/orchestrator.py")

        self.assertEqual(declared.returncode, ALLOW, declared.stderr)

        # The same file named absolutely inside the worktree, which is how an
        # agent rooted there refers to it.
        absolute = self._write(
            str(self.worktree.resolve() / ".ai" / "scripts" / "orchestrator.py")
        )

        self.assertEqual(absolute.returncode, ALLOW, absolute.stderr)

        undeclared = self._write(".ai/scripts/review-package.py")

        self.assertEqual(undeclared.returncode, DENY)
        self.assertIn("does not declare it", undeclared.stderr)

        # Declared by the worktree's decoy plan and by nothing the developer
        # approved: the authoritative plan is the checkout's.
        decoyed = self._write(".ai/hooks/pre_tool_use.py")

        self.assertEqual(decoyed.returncode, DENY)
        self.assertIn("does not declare it", decoyed.stderr)

    def test_a_plan_edited_after_approval_authorises_nothing(self):
        """Approval is bound to the plan's bytes, re-checked at write time."""
        (self.task_path / "plan.json").write_text(
            plan_json(
                files_to_modify=[
                    {"path": ".ai/scripts/orchestrator.py", "purpose": "edited"},
                    {"path": ".ai/scripts/review-package.py", "purpose": "new"},
                ]
            ),
            encoding="utf-8",
        )

        result = self._write(".ai/scripts/orchestrator.py")

        self.assertEqual(result.returncode, DENY)

    def test_ordinary_source_in_the_worktree_is_untouched(self):
        result = self._write("widget.py")

        self.assertEqual(result.returncode, ALLOW, result.stderr)


class DispatchTests(unittest.TestCase):
    def test_cost_is_read_only_and_unlocked(self):
        self.assertNotIn("cost", orch.ACTIONS)

    def test_usage_documents_cost(self):
        self.assertIn("cost", orch.USAGE)


if __name__ == "__main__":
    unittest.main()
