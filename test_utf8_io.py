"""TASK-006 - text I/O in the workflow scripts and hooks specifies UTF-8.

``Path.read_text()`` and ``Path.write_text()`` with no ``encoding=`` use the
locale encoding: cp1252 on the developer's Windows machine, UTF-8 on the Linux
CI runners. That is not hypothetical. ``orchestrator.py adr "<title>"`` wrote
its template's em-dash as ``?``, and every artifact an agent produces takes the
same path.

Two layers, because either alone is insufficient:

1. An **AST audit** of the four scoped files. A behavioural test cannot catch a
   regression here on a UTF-8 host -- the default encoding happens to be right
   there -- so the argument itself is asserted, statically, on every platform.
2. **Behavioural round trips** through the orchestrator's own helpers, which
   prove the encoding actually reaches the bytes on disk.

``.ai/hooks/pre_tool_use.py`` and ``.ai/hooks/post_tool_use.py`` already pass
``encoding="utf-8"`` everywhere and are deliberately out of scope.
"""

import ast
import unittest
from pathlib import Path

from _support import REPO_ROOT, TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()

# The files TASK-006 puts in scope.
SCOPED_FILES = (
    ".ai/scripts/orchestrator.py",
    ".ai/scripts/review-package.py",
    ".ai/hooks/session_start.py",
    ".ai/hooks/stop_guard.py",
)

# Text stream APIs: each opens or reads a decoded str and so takes an
# ``encoding``. ``os.open`` is excluded on purpose -- it returns a raw file
# descriptor, has no encoding to specify, and passing one is a TypeError.
TEXT_IO_NAMES = frozenset({"read_text", "write_text", "open", "fdopen"})

EM_DASH = "—"
CURLY_QUOTE = "“"
NON_LATIN = "日本語"  # Japanese, to leave the Latin-1 range entirely
REPLACEMENT = "�"

# A binary-mode stream decodes nothing, so it must not be given an encoding.
BINARY_MODES = ("rb", "wb", "ab", "r+b", "w+b", "br", "bw")


def call_label(node):
    """A readable ``file:line name`` for an assertion message."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "?")

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return "%s.%s" % (func.value.id, name)

    return name


def is_os_open(node):
    """``os.open`` creates a descriptor; it is not a text stream."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def is_binary_mode(node):
    mode = None

    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value

    if mode is None and node.args:
        # ``open``/``fdopen`` take mode positionally; ``read_text`` does not.
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value in BINARY_MODES:
                mode = first

    return isinstance(mode, ast.Constant) and mode.value in BINARY_MODES


def text_io_calls(path):
    """Every text stream call in one file, as ``(lineno, label, has_encoding)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)

        if name not in TEXT_IO_NAMES:
            continue

        if is_os_open(node) or is_binary_mode(node):
            continue

        has_encoding = any(keyword.arg == "encoding" for keyword in node.keywords)
        found.append((node.lineno, call_label(node), has_encoding))

    return found


def utf8_keyword(node):
    """Whether this call passes exactly ``encoding="utf-8"``."""
    for keyword in node.keywords:
        if keyword.arg != "encoding":
            continue

        return (
            isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
            and keyword.value.value.lower().replace("_", "-") == "utf-8"
        )

    return False


class Utf8SourceAuditTests(unittest.TestCase):
    """AC-1: every text stream call in the four scoped files declares UTF-8."""

    def test_scoped_files_exist(self):
        """A typo in a path would make the audit below vacuously pass."""
        for relative in SCOPED_FILES:
            self.assertTrue((REPO_ROOT / relative).is_file(), relative)

    def test_audit_actually_finds_calls(self):
        """Guard against an AST walk that silently matches nothing."""
        for relative in SCOPED_FILES:
            calls = text_io_calls(REPO_ROOT / relative)
            self.assertTrue(calls, "no text I/O calls detected in %s" % relative)

    def test_every_text_io_call_specifies_an_encoding(self):
        offenders = []

        for relative in SCOPED_FILES:
            for lineno, label, has_encoding in text_io_calls(REPO_ROOT / relative):
                if not has_encoding:
                    offenders.append("%s:%d %s" % (relative, lineno, label))

        self.assertEqual(offenders, [], "text I/O without encoding=: %s" % offenders)

    def test_every_encoding_is_utf8(self):
        """An encoding that is merely explicit is not enough; it must be UTF-8."""
        offenders = []

        for relative in SCOPED_FILES:
            path = REPO_ROOT / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )

                if name not in TEXT_IO_NAMES:
                    continue

                if is_os_open(node) or is_binary_mode(node):
                    continue

                if not utf8_keyword(node):
                    offenders.append(
                        "%s:%d %s" % (relative, node.lineno, call_label(node))
                    )

        self.assertEqual(offenders, [], "non-UTF-8 text I/O: %s" % offenders)

    def test_low_level_os_open_is_not_given_an_encoding(self):
        """``os.open(..., encoding=...)`` is a TypeError, not a fix."""
        path = REPO_ROOT / ".ai/scripts/orchestrator.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and is_os_open(node):
                self.assertFalse(
                    any(keyword.arg == "encoding" for keyword in node.keywords),
                    "os.open at line %d was given an encoding" % node.lineno,
                )


class Utf8RoundTripTests(TaskDirCase):
    """AC-2: non-ASCII survives the orchestrator's own artifact helpers."""

    unicode_content = (
        "Chose the %s option — see the %sdesign note%s — and %s."
        % ("wide", CURLY_QUOTE, "”", NON_LATIN)
    )

    def test_context_block_round_trips_unicode(self):
        orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="decision",
            content=self.unicode_content,
        )

        blocks = orch.read_context(self.task_id)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["content"], self.unicode_content)
        self.assertNotIn(REPLACEMENT, blocks[0]["content"])
        self.assertNotIn("?", blocks[0]["content"])

    def test_context_file_bytes_are_utf8(self):
        """Read the bytes back, so a lenient locale cannot mask the encoding."""
        orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="repo_finding",
            content=self.unicode_content,
        )

        raw = orch.context_file(self.task_id).read_bytes()

        # json.dump escapes non-ASCII by default, which is what keeps existing
        # JSONL artifacts pure ASCII. Decoding as UTF-8 must still work, and
        # the decoded text must not have lost anything.
        decoded = raw.decode("utf-8")

        self.assertNotIn(REPLACEMENT, decoded)

    def test_requirement_text_round_trips_through_task_creation(self):
        requirement = "Support %s names and an em-dash %s here.\n" % (
            NON_LATIN,
            EM_DASH,
        )

        with quiet():
            orch.create_task(requirement, task_id="TASK-998")

        path = Path(".ai") / "tasks" / "TASK-998" / "requirement.md"
        written = path.read_text(encoding="utf-8")

        self.assertIn(NON_LATIN, written)
        self.assertIn(EM_DASH, written)
        self.assertNotIn(REPLACEMENT, written)

        # Compare on the encoded characters, not on whole-file bytes: text-mode
        # write_text also translates "\n" to "\r\n" on Windows. That newline
        # behaviour is pre-existing and separate from the encoding under test.
        raw = path.read_bytes()

        self.assertIn(EM_DASH.encode("utf-8"), raw)
        self.assertIn(NON_LATIN.encode("utf-8"), raw)

    def test_ascii_content_is_byte_for_byte_unchanged(self):
        """The constraint that protects every artifact already in the repo."""
        ascii_content = "Plain ASCII decision, no punctuation tricks."

        orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="decision",
            content=ascii_content,
        )

        raw = orch.context_file(self.task_id).read_bytes()

        self.assertEqual(raw, raw.decode("ascii").encode("ascii"))


class Utf8AdrTests(TaskDirCase):
    """AC-3: the observed defect. The ADR template's em-dash must survive."""

    def test_adr_supersedes_line_has_a_literal_em_dash(self):
        with quiet():
            self.assertEqual(orch.run_adr("Text I/O specifies UTF-8"), 0)

        records = sorted((Path(".ai") / "adr").glob("*.md"))

        self.assertEqual(len(records), 1, "expected exactly one ADR")

        text = records[0].read_text(encoding="utf-8")
        line = next(
            one for one in text.splitlines() if one.startswith("- **Supersedes:**")
        )

        self.assertEqual(line, "- **Supersedes:** " + EM_DASH)

    def test_adr_bytes_contain_no_replacement_or_question_mark(self):
        """The exact corruption seen on Windows: the em-dash became ``?``."""
        with quiet():
            self.assertEqual(orch.run_adr("Another decision"), 0)

        record = sorted((Path(".ai") / "adr").glob("*.md"))[0]
        raw = record.read_bytes()

        self.assertIn(EM_DASH.encode("utf-8"), raw)
        self.assertNotIn(REPLACEMENT.encode("utf-8"), raw)
        self.assertNotIn(b"Supersedes:** ?", raw)

    def test_adr_title_round_trips_non_ascii(self):
        with quiet():
            self.assertEqual(orch.run_adr("Naming for %s modules" % NON_LATIN), 0)

        record = sorted((Path(".ai") / "adr").glob("*.md"))[0]

        self.assertIn(NON_LATIN, record.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
