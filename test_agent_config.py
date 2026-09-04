"""Fixes B7, B8 / audit 2.9, 2.10 - correct worker, declared tools, sane mode.

The orchestrator invoked ``--agent lead-agent``, which declares only
``Read, Grep, Glob, Bash``. So the implementation worker either could not edit
files or wrote them through Bash heredocs, routing every file mutation around
the tool-level permission surface. It also collapsed the orchestration role
into the implementation role -- the separation the repo is built around.
"""

import unittest
from unittest import mock

from _support import REPO_ROOT, TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()

AGENTS = REPO_ROOT / ".claude" / "agents"


def declared_tools(agent_file):
    """Parse the frontmatter ``tools:`` line of an agent definition."""
    text = agent_file.read_text()

    if not text.startswith("---"):
        raise AssertionError("agent file has no frontmatter: " + str(agent_file))

    frontmatter = text.split("---", 2)[1]

    for line in frontmatter.splitlines():
        if line.startswith("tools:"):
            return {
                part.strip()
                for part in line.split(":", 1)[1].split(",")
                if part.strip()
            }

    return set()


class ImplementerAgentTests(unittest.TestCase):
    def test_implementer_agent_exists(self):
        self.assertTrue((AGENTS / "implementer.md").is_file())

    def test_implementer_declares_write_tools(self):
        tools = declared_tools(AGENTS / "implementer.md")

        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)
        self.assertIn("Read", tools)

    def test_lead_agent_remains_read_only(self):
        tools = declared_tools(AGENTS / "lead-agent.md")

        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)

    def test_implementer_forbids_orchestrator_owned_artifacts(self):
        body = (AGENTS / "implementer.md").read_text()

        self.assertIn("state.json", body)
        self.assertIn("test-results.json", body)


class InvocationTests(TaskDirCase):
    def _capture_invocation(self):
        self.write_state()
        self.write_requirement()
        self.write_plan()

        with quiet():
            orch.approve_plan(self.task_id)

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_implementation(self.task_id)

        return captured

    def test_invokes_the_implementer_agent(self):
        argv = self._capture_invocation()["argv"]

        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "implementer")
        self.assertNotIn("lead-agent", argv)

    def test_uses_dontask_not_auto(self):
        argv = self._capture_invocation()["argv"]

        self.assertIn("--permission-mode", argv)
        self.assertEqual(
            argv[argv.index("--permission-mode") + 1], "dontAsk"
        )
        self.assertNotIn("auto", argv)

    def test_passes_an_explicit_tool_allowlist(self):
        argv = self._capture_invocation()["argv"]

        self.assertIn("--allowedTools", argv)
        allowed = argv[argv.index("--allowedTools") + 1]

        self.assertTrue(allowed.strip())
        self.assertIn("Edit", allowed)
        self.assertIn("Write", allowed)

    def test_allowlist_matches_the_agent_declaration(self):
        """The CLI allowlist must not grant more than the agent declares."""
        allowed = set(orch.CLAUDE_ALLOWED_TOOLS.split(","))
        declared = declared_tools(AGENTS / "implementer.md")

        self.assertTrue(
            allowed.issubset(declared),
            "allowlist grants tools the agent does not declare: "
            + str(sorted(allowed - declared)),
        )

    def test_implementation_call_has_a_timeout(self):
        kwargs = self._capture_invocation()["kwargs"]

        self.assertEqual(kwargs.get("timeout"), orch.CLAUDE_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
