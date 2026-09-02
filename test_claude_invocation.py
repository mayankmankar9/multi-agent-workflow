"""How the Claude CLI is actually invoked.

`--allowedTools` is declared variadic -- `<tools...>`, comma *or* space
separated -- so a prompt passed as a trailing argument after it is parsed as
another tool name. The CLI then has no prompt and exits 1:

    Error: Input must be provided either through stdin or as a prompt argument
    when using --print

Every `claude` invocation in the orchestrator was built that way: the three
reviewers, the plan critic, the failure classifier, the clarifier and the
implementer. None of them had ever run. The mocked tests all passed, because a
mock does not parse argv.

It surfaced on the first real end-to-end task as:

    PLAN_CRITIQUED ran=false
    note: agent plan-critic exited 1: Error: Input must be provided ...

which is the "did not run is not found nothing" rule doing its job -- the
failure was recorded honestly instead of becoming an empty critique. That is
what made it findable, but only because the record was read.

The prompt now goes on stdin, so argument order cannot reintroduce this.
"""

import unittest
from unittest import mock

from _support import load_orchestrator, worker_name

orch = load_orchestrator()

# Flags the CLI declares as variadic. A positional argument placed after any of
# these is swallowed by it.
VARIADIC_FLAGS = ("--allowedTools", "--allowed-tools", "--disallowedTools")


class ClaudeArgvTests(unittest.TestCase):
    def test_carries_no_positional_prompt(self):
        argv = orch.claude_argv("reviewer-security", orch.READONLY_TOOLS)

        self.assertEqual(argv[-1], orch.READONLY_TOOLS)

    def test_nothing_follows_the_variadic_tool_flag(self):
        """The regression, stated directly."""
        argv = orch.claude_argv("reviewer-security", orch.READONLY_TOOLS)

        for flag in VARIADIC_FLAGS:
            if flag not in argv:
                continue

            index = argv.index(flag)
            # Exactly one value, and it is the last element.
            self.assertEqual(len(argv), index + 2, flag)

    def test_passes_the_agent_and_permission_mode(self):
        argv = orch.claude_argv("plan-critic", orch.READONLY_TOOLS)

        self.assertEqual(argv[argv.index("--agent") + 1], "plan-critic")
        self.assertEqual(
            argv[argv.index("--permission-mode") + 1],
            orch.CLAUDE_PERMISSION_MODE,
        )

    def test_uses_print_mode(self):
        self.assertIn("--print", orch.claude_argv("a", "Read"))

    def test_resolves_the_executable(self):
        argv = orch.claude_argv("a", "Read")

        self.assertEqual(worker_name(argv), "claude")


class StructuredAgentStdinTests(unittest.TestCase):
    def test_the_prompt_is_delivered_on_stdin(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_structured_agent("reviewer-security", "SENTINEL", 10)

        self.assertEqual(seen["input"], "SENTINEL")
        self.assertNotIn("SENTINEL", seen["argv"])

    def test_stdin_is_text_mode(self):
        """A str prompt with text=False raises rather than running."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_structured_agent("reviewer-security", "p", 10)

        self.assertTrue(seen.get("text"))


class RunWorkerStdinTests(unittest.TestCase):
    def _capture(self, **kwargs):
        seen = {}

        def fake_run(argv, **call):
            seen.update(call)
            seen["argv"] = argv
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude"], 10, **kwargs)

        return seen

    def test_stdin_text_is_passed_through(self):
        self.assertEqual(self._capture(stdin_text="P")["input"], "P")

    def test_text_mode_is_enabled_for_a_string_prompt(self):
        """Without this subprocess demands bytes and the run dies on the prompt."""
        self.assertTrue(self._capture(stdin_text="P")["text"])

    def test_no_stdin_keeps_the_previous_behaviour(self):
        seen = self._capture()

        self.assertIsNone(seen["input"])
        self.assertIsNone(seen["text"])

    def test_capture_still_implies_text_mode(self):
        self.assertTrue(self._capture(capture=True)["text"])


class ImplementerInvocationTests(unittest.TestCase):
    def test_prompt_reaches_the_worker_on_stdin(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.invoke_implementer("SENTINEL_PROMPT")

        self.assertEqual(seen["input"], "SENTINEL_PROMPT")
        self.assertNotIn("SENTINEL_PROMPT", seen["argv"])

    def test_nothing_follows_the_variadic_tool_flag(self):
        argv = orch.implementer_argv()

        index = argv.index("--allowedTools")

        self.assertEqual(len(argv), index + 2)


class PipeEncodingTests(unittest.TestCase):
    """A worker's output is UTF-8. The locale codec is not.

    `subprocess.run(..., text=True)` decodes with the locale encoding, which is
    cp1252 on this developer's Windows machine. The first real end-to-end run
    recorded the failure classifier's own rationale into events.jsonl with an
    em dash mangled to mojibake -- the file write was already UTF-8 by then, so
    the corruption happened on the way *in*, before anything could be written
    correctly.

    Corrupting evidence while recording it is worse than not recording it: the
    ledger looked complete and was wrong.
    """

    def test_structured_agent_decodes_as_utf8(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_structured_agent("reviewer-security", "p", 10)

        self.assertEqual(seen.get("encoding"), "utf-8")

    def test_run_worker_decodes_as_utf8_when_capturing(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stdout="", stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude"], 10, capture=True)

        self.assertEqual(seen.get("encoding"), "utf-8")

    def test_run_worker_leaves_encoding_unset_in_binary_mode(self):
        """encoding= forces text mode, which would break the non-capturing path."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude"], 10)

        self.assertIsNone(seen.get("encoding"))

    def test_no_text_pipe_is_left_on_the_locale_codec(self):
        """Every text=True subprocess call must name its encoding."""
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )
        lines = source.splitlines()
        offenders = []

        for index, line in enumerate(lines):
            if "text=True" not in line:
                continue

            # Wide enough to span an intervening comment; the encoding always
            # sits in the same subprocess.run(...) call, which ends at ")".
            window = []

            for candidate in lines[index : index + 12]:
                window.append(candidate)

                if candidate.strip() == ")":
                    break

            if "encoding=" not in " ".join(window):
                offenders.append(index + 1)

        self.assertEqual(offenders, [], "text=True without encoding= at these lines")


class ImplementerPromptTests(unittest.TestCase):
    """The prompt must not contradict the plan it is delivering."""

    def test_scope_is_stated_relative_to_the_plan(self):
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("files_to_modify and", source)

    def test_no_blanket_ban_on_changing_the_orchestrator(self):
        """This repo's own source IS the orchestrator, so a blanket ban made
        every self-hosted task unimplementable -- the worker was told to refuse
        exactly what the approved plan told it to do."""
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        self.assertFalse(
            "- Do not change the orchestrator." in source,
            "blanket ban still present in an implementer or fix prompt",
        )


if __name__ == "__main__":
    unittest.main()
