"""Hooks must not be defeated by their own input.

TASK-006 made the hooks read with a strict UTF-8 codec. That was right, and it
introduced two failure modes the reviewers caught:

- `stop_guard.py` read worker-writable artifacts with no handler, so malformed
  bytes raised, the script exited 1, and **exit 1 blocks nothing** -- the
  write-back guard silently did not run. The strict codec made this worse than
  the locale decode it replaced: cp1252 rejects five byte values, UTF-8 rejects
  any malformed high-byte sequence.
- `session_start.py` read UTF-8 and then wrote through `sys.stdout`, which uses
  the locale codec. A block containing anything outside cp1252 raised
  UnicodeEncodeError and the blackboard injection the hook exists to guarantee
  was lost -- the same failure mode TASK-006 set out to remove.

A guard fails closed. An injector degrades loudly. Neither dies quietly.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import REPO_ROOT

HOOKS = REPO_ROOT / ".ai" / "hooks"


def load_hook(filename, module_name):
    """Import a hook so its functions can be called directly.

    ``_support.load_script`` reaches ``.ai/scripts`` only, and some branches
    cannot be reached from a subprocess at all: a real child process always has
    a stdout with a ``.buffer``, so the fallback in ``emit`` is unreachable from
    the tests below that run the hook as the harness does.
    """
    path = HOOKS / filename
    spec = importlib.util.spec_from_file_location(module_name, path)

    if spec is None or spec.loader is None:
        raise ImportError("Cannot load %s" % path)

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

BLOCK = 2
ALLOW = 0

# Valid in cp1252, invalid as UTF-8: a lone 0x81 continuation byte.
INVALID_UTF8 = b"note\x81\x81 broken\n"

# Outside cp1252 entirely, so a locale-encoded stdout cannot emit it.
NON_CP1252 = "日本語"


class HookCase(unittest.TestCase):
    """Each test gets a throwaway repo-shaped tree."""

    task_id = "TASK-999"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-hook-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.task_path = self.tmp / ".ai" / "tasks" / self.task_id
        self.task_path.mkdir(parents=True)

    def run_hook(self, script, env=None):
        """Run a hook as the harness does, capturing raw bytes.

        PYTHONIOENCODING and PYTHONUTF8 are cleared deliberately: either one
        would make stdout UTF-8 regardless of the code under test and turn the
        encoding assertions below into tautologies.

        The three rooting variables are *assigned*, never inherited. The hooks
        prefer an exported absolute path over cwd, so inside an orchestrated
        run the ambient values would point every fixture below at the live task
        tree: the throwaway tree would be ignored and the cases would report on
        evidence they did not write. Per-test overrides are applied last, so a
        case that wants different rooting -- or a hostile ambient value -- can
        still say so.
        """
        environment = dict(os.environ)
        environment["ORCHESTRATOR_TASK_ID"] = self.task_id
        environment["ORCHESTRATOR_REPO_ROOT"] = str(self.tmp)
        environment["ORCHESTRATOR_TASK_DIR"] = str(self.task_path)
        environment["ORCHESTRATOR_CONSTITUTION"] = str(
            self.tmp / ".ai" / "constitution.md"
        )
        environment.pop("PYTHONIOENCODING", None)
        environment.pop("PYTHONUTF8", None)
        environment.update(env or {})

        return subprocess.run(
            [sys.executable, str(HOOKS / script)],
            input=b"",
            cwd=str(self.tmp),
            capture_output=True,
            env=environment,
        )

    def exported_tree(self):
        """A second task tree, elsewhere on disk, to point a hook at.

        Deliberately not under ``self.tmp``: cwd resolution cannot reach it, so
        a case asserting on this tree's contents is asserting that the exported
        path is what won.
        """
        root = Path(tempfile.mkdtemp(prefix="wf-exported-"))
        self.addCleanup(shutil.rmtree, root, True)

        path = root / ".ai" / "tasks" / self.task_id
        path.mkdir(parents=True)
        return path

    def write_notes(self, content="Did the thing.\n", where=None):
        path = (where or self.task_path) / "implementation.md"

        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

        return path

    def write_block(
        self, content="Learned a thing.", author="implementer", where=None
    ):
        payload = {
            "block_id": "ctx-001",
            "type": "repo_finding",
            "author": author,
            "phase": "IMPLEMENTING",
            "content": content,
        }
        path = (where or self.task_path) / "context.jsonl"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def write_raw_block(self, raw, where=None):
        path = (where or self.task_path) / "context.jsonl"
        path.write_bytes(raw)
        return path


class StopGuardFailsClosedTests(HookCase):
    def test_undecodable_notes_block_the_session(self):
        """The regression: this used to exit 1, which blocks nothing."""
        self.write_notes(INVALID_UTF8)
        self.write_block()

        result = self.run_hook("stop_guard.py")

        self.assertEqual(result.returncode, BLOCK)

    def test_undecodable_notes_say_why(self):
        self.write_notes(INVALID_UTF8)
        self.write_block()

        result = self.run_hook("stop_guard.py")

        self.assertIn(b"not valid UTF-8", result.stderr)

    def test_undecodable_blackboard_blocks_the_session(self):
        self.write_notes()
        self.write_raw_block(INVALID_UTF8)

        result = self.run_hook("stop_guard.py")

        self.assertEqual(result.returncode, BLOCK)

    def test_it_never_exits_one(self):
        """Exit 1 is the silent-failure code; a refusal must never use it."""
        self.write_notes(INVALID_UTF8)
        self.write_block()

        self.assertNotEqual(self.run_hook("stop_guard.py").returncode, 1)

    def test_a_complete_write_back_still_passes(self):
        self.write_notes()
        self.write_block()

        self.assertEqual(self.run_hook("stop_guard.py").returncode, ALLOW)

    def test_a_missing_write_back_still_blocks(self):
        """The guard's original job must survive the hardening."""
        self.write_notes()

        self.assertEqual(self.run_hook("stop_guard.py").returncode, BLOCK)

    def test_non_ascii_notes_are_read_not_rejected(self):
        """Valid UTF-8 outside cp1252 is normal input, not a fault."""
        self.write_notes("Handled %s correctly.\n" % NON_CP1252)
        self.write_block()

        self.assertEqual(self.run_hook("stop_guard.py").returncode, ALLOW)


class SessionStartEmitsUtf8Tests(HookCase):
    def test_non_cp1252_content_does_not_crash_the_hook(self):
        """The regression: sys.stdout would raise UnicodeEncodeError here."""
        self.write_block(content="Chose %s for the label." % NON_CP1252)

        result = self.run_hook("session_start.py")

        self.assertEqual(result.returncode, 0, result.stderr[:400])

    def test_non_cp1252_content_actually_reaches_stdout(self):
        """Not crashing is not enough; the injection must be there."""
        self.write_block(content="Chose %s for the label." % NON_CP1252)

        result = self.run_hook("session_start.py")

        self.assertIn(NON_CP1252.encode("utf-8"), result.stdout)

    def test_stdout_is_utf8_not_the_locale_codec(self):
        self.write_block(content=NON_CP1252)

        result = self.run_hook("session_start.py")
        decoded = result.stdout.decode("utf-8")

        self.assertIn(NON_CP1252, decoded)
        self.assertNotIn("�", decoded)

    def test_ascii_content_is_unaffected(self):
        self.write_block(content="Plain ASCII finding.")

        result = self.run_hook("session_start.py")

        self.assertEqual(result.returncode, 0)
        self.assertIn(b"Plain ASCII finding.", result.stdout)

    def test_stdout_without_a_buffer_still_emits(self):
        """The branch the subprocess tests above cannot reach.

        ``emit`` writes bytes to ``sys.stdout.buffer``, which is what keeps the
        locale codec out of it. A stdout replaced by an embedding harness -- a
        captured stream, another agent runtime -- may have no buffer, and that
        fallback decides whether the injection is written at all or lost to an
        AttributeError. The claim here is only that the text arrives intact and
        nothing raises; a caller's own stream can still have a lossy codec of
        its own, and this cannot speak for that.
        """
        module = load_hook("session_start.py", "hook_session_start")
        captured = io.StringIO()

        self.assertIsNone(
            getattr(captured, "buffer", None), "premise: this stdout has no buffer"
        )

        with contextlib.redirect_stdout(captured):
            module.emit("Chose %s for the label.\n" % NON_CP1252)

        written = captured.getvalue()

        self.assertIn(NON_CP1252, written)
        self.assertNotIn("�", written)


class SessionStartDegradesLoudlyTests(HookCase):
    def test_an_undecodable_blackboard_does_not_crash(self):
        """An injector must not block, so it degrades instead."""
        self.write_raw_block(INVALID_UTF8)

        result = self.run_hook("session_start.py")

        self.assertEqual(result.returncode, 0)

    def test_an_undecodable_blackboard_is_reported(self):
        """Degrading silently would hide a lost injection."""
        self.write_raw_block(INVALID_UTF8)

        result = self.run_hook("session_start.py")

        self.assertIn(b"not valid UTF-8", result.stderr)

    def test_it_stays_quiet_outside_a_task(self):
        """Ordinary sessions in this repo must be unaffected."""
        result = self.run_hook("session_start.py", env={"ORCHESTRATOR_TASK_ID": ""})

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")


class HookRootingResilienceInteractionTests(HookCase):
    """Where the evidence is, and whether the hook survives reading it.

    Neither half carries these on its own. The cwd tree is given complete,
    decodable evidence, so a hook that resolved against cwd would report a
    clean pass and never see the fault the exported tree carries -- and the
    fault is an undecodable byte, so a hook without its resilience helper dies
    on it instead of ruling on it.
    """

    def test_stop_guard_fails_closed_on_undecodable_exported_task_artifact(self):
        # Complete and decodable, so cwd resolution cannot block for the
        # unrelated reason that it found nothing.
        self.write_notes()
        self.write_block()

        exported = self.exported_tree()
        self.write_block(where=exported)
        broken = self.write_notes(INVALID_UTF8, where=exported)

        result = self.run_hook(
            "stop_guard.py", env={"ORCHESTRATOR_TASK_DIR": str(exported)}
        )
        stderr = result.stderr.decode("utf-8", errors="replace")

        self.assertEqual(result.returncode, BLOCK, stderr[:400])
        self.assertNotEqual(result.returncode, 1)
        self.assertIn("not valid UTF-8", stderr)
        self.assertIn(str(broken), stderr)
        self.assertNotIn(str(self.task_path / "implementation.md"), stderr)

    def test_session_start_injects_non_cp1252_exported_blackboard_under_cp1252_stdout(
        self,
    ):
        self.write_block(content="Local cwd finding, not the exported one.")

        exported = self.exported_tree()
        self.write_block(
            content="Chose %s for the label." % NON_CP1252, where=exported
        )

        result = self.run_hook(
            "session_start.py",
            env={
                "ORCHESTRATOR_TASK_DIR": str(exported),
                # Force the codec emit() must not consult. Under a UTF-8
                # validation locale sys.stdout would encode this blackboard
                # fine, and the criterion would prove nothing about emit().
                "PYTHONIOENCODING": "cp1252",
            },
        )
        stderr = result.stderr.decode("utf-8", errors="replace")

        self.assertEqual(result.returncode, 0, stderr[:400])
        self.assertIn(NON_CP1252.encode("utf-8"), result.stdout)
        self.assertNotIn(b"Local cwd finding", result.stdout)


class ExportedConstitutionResilienceTests(HookCase):
    """The constitution is located by export and read like any other input."""

    def constitution_outside_the_repo(self, content):
        root = Path(tempfile.mkdtemp(prefix="wf-constitution-"))
        self.addCleanup(shutil.rmtree, root, True)

        path = root / "constitution.md"

        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

        return path

    def test_absent_constitution_is_silent(self):
        """A checkout without one is not a fault, so nothing is said."""
        self.write_block()

        result = self.run_hook(
            "session_start.py",
            env={
                "ORCHESTRATOR_CONSTITUTION": str(
                    self.tmp / "nowhere" / "constitution.md"
                )
            },
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b"Project invariants", result.stdout)
        self.assertNotIn(b"constitution", result.stderr)
        self.assertIn(b"Learned a thing", result.stdout)

    def test_undecodable_constitution_warns(self):
        """One that exists and cannot be read is, so it degrades loudly."""
        self.write_block()
        constitution = self.constitution_outside_the_repo(INVALID_UTF8)

        result = self.run_hook(
            "session_start.py",
            env={"ORCHESTRATOR_CONSTITUTION": str(constitution)},
        )
        stderr = result.stderr.decode("utf-8", errors="replace")

        self.assertEqual(result.returncode, 0, stderr[:400])
        self.assertIn("not valid UTF-8", stderr)
        self.assertIn(str(constitution), stderr)
        # The rest of the injection survives the gap.
        self.assertIn(b"Learned a thing", result.stdout)

    def test_readable_exported_constitution_uses_relative_label(self):
        """The heading names the repo-relative path, not the exported one.

        The exported path is absolute and, here, outside the repository
        entirely. Labelling the section with it would leak a worker-irrelevant
        absolute path into every injected context, which is why the label
        stayed a constant when the lookup became an export.
        """
        constitution = self.constitution_outside_the_repo(
            "Report only what you observed.\n"
        )

        result = self.run_hook(
            "session_start.py",
            env={"ORCHESTRATOR_CONSTITUTION": str(constitution)},
        )
        stdout = result.stdout.decode("utf-8")

        self.assertEqual(result.returncode, 0, result.stderr[:400])
        self.assertIn("Report only what you observed.", stdout)
        self.assertIn(
            "Project invariants (%s)" % os.path.join(".ai", "constitution.md"),
            stdout,
        )
        self.assertNotIn(str(constitution), stdout)


class HookHarnessIsolationTests(unittest.TestCase):
    """The harness must not hand the hooks the ambient orchestrator's tree.

    ``run_hook`` inherited the three rooting variables. Inside an orchestrated
    run those name real, complete evidence, and the hooks prefer an exported
    path over cwd -- so every fixture in this module would have been answered
    from the live task tree, passing while saying nothing about the code under
    test. The decoy below stands in for that tree: complete notes, a valid
    block, a readable constitution. Inheriting it inverts every assertion here.
    """

    def setUp(self):
        self.decoy = Path(tempfile.mkdtemp(prefix="wf-decoy-"))
        self.addCleanup(shutil.rmtree, self.decoy, True)

        self.decoy_task = self.decoy / ".ai" / "tasks" / HookCase.task_id
        self.decoy_task.mkdir(parents=True)

        (self.decoy_task / "implementation.md").write_text(
            "Decoy notes, complete enough to pass the guard.\n", encoding="utf-8"
        )
        (self.decoy_task / "context.jsonl").write_text(
            json.dumps(
                {
                    "block_id": "ctx-001",
                    "type": "repo_finding",
                    "author": "implementer",
                    "phase": "IMPLEMENTING",
                    "content": "Decoy block.",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.decoy / ".ai" / "constitution.md").write_text(
            "Decoy invariants.\n", encoding="utf-8"
        )

        self.hostile = {
            "ORCHESTRATOR_TASK_DIR": str(self.decoy_task),
            "ORCHESTRATOR_REPO_ROOT": str(self.decoy),
            "ORCHESTRATOR_CONSTITUTION": str(
                self.decoy / ".ai" / "constitution.md"
            ),
        }

    def fixture(self):
        """A ``HookCase`` driven directly rather than through the loader.

        ``run_hook`` is named only because ``TestCase.__init__`` requires an
        existing attribute; it does not match the test prefix, so nothing
        collects it as a case.
        """
        case = HookCase("run_hook")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_run_hook_overrides_hostile_ambient_rooting_variables(self):
        with mock.patch.dict(os.environ, self.hostile):
            case = self.fixture()
            # Its own tree: notes, and deliberately no block. The decoy has
            # both, so reading the decoy would allow the session to end.
            case.write_notes()

            blocked = case.run_hook("stop_guard.py")

            # With the task dir cleared, the other two variables decide where
            # the blackboard and the constitution come from.
            injected = case.run_hook(
                "session_start.py", env={"ORCHESTRATOR_TASK_DIR": ""}
            )

        stderr = blocked.stderr.decode("utf-8", errors="replace")

        self.assertEqual(blocked.returncode, BLOCK, stderr[:400])
        self.assertIn(str(case.task_path / "context.jsonl"), stderr)
        self.assertNotIn(str(self.decoy_task), stderr)

        self.assertEqual(injected.returncode, 0, injected.stderr[:400])
        self.assertEqual(injected.stdout, b"")


class HookExitCodeContractTests(unittest.TestCase):
    """Both hooks are held to the exit-code rule in their own docstrings."""

    def test_stop_guard_never_returns_one(self):
        source = (HOOKS / "stop_guard.py").read_text(encoding="utf-8")

        self.assertNotIn("return 1", source)

    def test_session_start_never_returns_two(self):
        """SessionStart must not block a session from starting."""
        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")

        self.assertNotIn("return 2", source)

    def test_stop_guard_reads_only_through_the_guarded_helper(self):
        """A second bare read_text is the defect returning.

        One occurrence is expected and required: the one inside read_guarded.
        """
        source = (HOOKS / "stop_guard.py").read_text(encoding="utf-8")

        self.assertEqual(
            source.count(".read_text("),
            1,
            "every guarded read must go through read_guarded",
        )

    def test_session_start_reads_only_through_its_helper(self):
        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")

        self.assertEqual(
            source.count(".read_text("),
            1,
            "every injected read must go through read_or_warn",
        )

    def test_session_start_encodes_its_output_explicitly(self):
        """Writing the joined parts straight to sys.stdout is the defect."""
        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")

        self.assertIn('encode("utf-8"', source)


if __name__ == "__main__":
    unittest.main()
