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
import contextlib
import json
import os
import sys
import types
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

# The files TASK-007 changes, held to the oldest interpreter the CI matrix runs.
TOUCHED_SOURCES = ("test_utf8_io.py", "test_hook_resilience.py")

ORCHESTRATOR_PATH = REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"

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
    """Whether this call opens a binary stream, which takes no encoding.

    The mode's position varies with the call: ``Path.open("rb")`` puts it
    first, while ``open(path, "rb")`` and ``os.fdopen(fd, "rb")`` put it
    second. Inspecting only ``args[0]`` therefore classified both of the
    latter as *text* I/O and reported them as offenders for lacking an
    encoding -- a hard failure for legitimate binary I/O, and the exact
    inverse of the plan's stated risk.

    So every positional argument is considered. A path or descriptor is never
    spelled ``"rb"``, so scanning them all costs nothing and stops the check
    depending on which spelling a call happens to use.
    """
    mode = None

    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value

    if mode is None:
        for argument in node.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value in BINARY_MODES
            ):
                mode = argument
                break

    return isinstance(mode, ast.Constant) and mode.value in BINARY_MODES


def text_io_calls(path):
    """Every text stream call in one file, as ``(lineno, label, has_encoding)``.

    Results are cached per path: five tests audit the same four files, and
    orchestrator.py is ~4000 lines, so the uncached version read and re-parsed
    the same sources twenty times to reach the same answer.
    """
    key = str(path)

    if key in _CALL_CACHE:
        return _CALL_CACHE[key]

    found = calls_in_source(path.read_text(encoding="utf-8"), str(path))
    _CALL_CACHE[key] = found
    return found


_CALL_CACHE = {}


def calls_in_source(source, filename="<source>"):
    """The audit itself, over source text rather than a path.

    Split out so the audit can be run against a deliberately broken copy of a
    file without writing that copy to disk -- which is how
    ``AuditDiscriminatesTests`` proves the audit can actually fail.
    """
    tree = ast.parse(source, filename=filename)
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


# The two production calls the blackboard round trip depends on. Each is
# asserted unique before it is cut, so a mutation lands on the call the test
# names rather than on something that merely looks like it.
CONTEXT_WRITE_CALL = 'context_file(task_id).open("a", encoding="utf-8")'
CONTEXT_WRITE_MUTANT = 'context_file(task_id).open("a")'
CONTEXT_READ_CALL = 'for line in path.read_text(encoding="utf-8").splitlines():'
CONTEXT_READ_MUTANT = "for line in path.read_text().splitlines():"

_MUTANT_SERIAL = [0]


class UnescapedJson:
    """``json`` whose ``dump`` defaults to ``ensure_ascii=False``.

    ``append_context_block`` serialises with ``ensure_ascii=True``, so
    context.jsonl is pure ASCII on disk however it is encoded -- which is why
    the round-trip tests named below could not fail, and passed with the fix
    removed. Substituting this for one module's ``json`` puts real multi-byte
    characters in the file, so the encoding argument becomes the thing under
    test. Production serialisation is untouched: the substitution is on the
    module object handed to a single test and is undone when it ends.
    """

    def __init__(self, real):
        self._real = real

    def dump(self, obj, fp, **kwargs):
        kwargs.setdefault("ensure_ascii", False)
        return self._real.dump(obj, fp, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def orchestrator_module(source, label):
    """Execute orchestrator source as a throwaway module, without writing it.

    The mutation tests need a module whose bytes differ from the file on disk.
    Exec'ing in a fresh namespace keeps the broken copy off the filesystem --
    the same reason ``calls_in_source`` takes source text rather than a path --
    and gives each test its own module, so no mutant can leak into another.
    """
    _MUTANT_SERIAL[0] += 1
    name = "workflow_orchestrator_%s_%d" % (label, _MUTANT_SERIAL[0])
    module = types.ModuleType(name)
    module.__file__ = str(ORCHESTRATOR_PATH)
    sys.modules[name] = module

    try:
        exec(compile(source, str(ORCHESTRATOR_PATH), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise

    return module


@contextlib.contextmanager
def omitted_encoding_is_utf16():
    """Give a call with no ``encoding=`` a deterministic, hostile default.

    Removing ``encoding="utf-8"`` falls back to the locale codec, which on a
    UTF-8 host *is* UTF-8 -- so a mutant would pass on the Linux runners while
    failing on Windows, and the mutation test would prove different things on
    different machines. Pinning the fallback to UTF-16 makes the removed
    argument observable everywhere.

    Explicit encodings and binary modes are passed straight through, so a
    failure under this shim can only come from the argument the test removed.
    """
    original = Path.open

    def open_with_utf16_default(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        # 3.10+ resolves an omitted encoding to the sentinel "locale" before
        # Path.open sees it; 3.9 passes None through.
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "utf-16"

        return original(self, mode, buffering, encoding, errors, newline)

    Path.open = open_with_utf16_default

    try:
        yield
    finally:
        Path.open = original


def run_round_trip(method, module):
    """Run one ``Utf8RoundTripTests`` method against a given orchestrator.

    The round-trip tests reach the orchestrator through this module's ``orch``
    global, so binding a different module there is what points them at a
    mutant. Running through ``TestCase.run`` keeps setUp, the temp tree and the
    cleanups exactly as they are in a normal run.
    """
    previous = globals()["orch"]
    globals()["orch"] = module
    result = unittest.TestResult()

    try:
        Utf8RoundTripTests(method).run(result)
    finally:
        globals()["orch"] = previous

    return result


def why_it_failed(result):
    """The last line of each failure, for an assertion message."""
    entries = list(result.failures) + list(result.errors)

    return "; ".join(text.strip().splitlines()[-1] for _, text in entries)


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

    def write_real_high_bytes(self):
        """Make this test's blackboard writes land as non-ASCII bytes.

        Scoped to the module the test is actually running against, which is a
        mutant when ``AuditDiscriminatesTests`` is driving, and undone when the
        test ends however it ends.
        """
        module = orch
        real = module.json
        module.json = UnescapedJson(real)
        self.addCleanup(setattr, module, "json", real)

    def test_context_block_round_trips_unicode(self):
        """AC-2's behavioural claim, over bytes that can actually break.

        Restored under its original name after TASK-007 found the version it
        replaced could not fail: with ensure_ascii=True the file is pure ASCII,
        so the round trip held with encoding= removed on every host. The
        fixture now writes genuine multi-byte characters, so both the write
        encoding and the read encoding have to be right for the block to come
        back -- and AuditDiscriminatesTests removes each in turn and requires
        this test to fail.
        """
        self.write_real_high_bytes()

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

        # The premise the test rests on: without high bytes on disk, the round
        # trip would hold whatever encoding produced them.
        raw = orch.context_file(self.task_id).read_bytes()

        self.assertTrue(
            any(byte > 0x7F for byte in raw), "fixture wrote no non-ASCII bytes"
        )

    def test_context_file_bytes_are_utf8(self):
        """The bytes on disk are UTF-8, asserted where that can be false.

        Also restored: the version this replaces decoded pure-ASCII bytes as
        UTF-8, which cannot fail. With the fixture forcing real high bytes, the
        decode and the two membership checks are all claims about the write
        encoding.
        """
        self.write_real_high_bytes()

        orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="repo_finding",
            content=self.unicode_content,
        )

        raw = orch.context_file(self.task_id).read_bytes()

        self.assertIn(NON_LATIN.encode("utf-8"), raw)
        self.assertIn(CURLY_QUOTE.encode("utf-8"), raw)
        self.assertNotIn(REPLACEMENT.encode("utf-8"), raw)
        self.assertIn(self.unicode_content, raw.decode("utf-8"))

    def test_context_block_survives_json_escaping(self):
        """JSON-escape fidelity, which is *not* the encoding claim.

        Split off from test_context_block_round_trips_unicode, which read as
        encoding coverage and was not: append_context_block serialises through
        json.dumps with the default ensure_ascii=True, so the bytes on disk are
        pure ASCII whatever encoding= is passed, and this passed with the fix
        removed on any host.

        The behaviour is still worth pinning -- ensure_ascii round-tripping is
        what keeps existing JSONL artifacts readable -- so it stays here, under
        a name that claims only what it checks, while the encoding claim lives
        in the restored test above.
        """
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

    def test_context_file_is_pure_ascii_by_json_escaping(self):
        """Also not the encoding claim; states the real invariant instead.

        Split off from test_context_file_bytes_are_utf8, whose UTF-8 decode of
        pure-ASCII bytes could not fail. What is actually true, and worth
        holding, is that the blackboard stays ASCII on disk under production
        serialisation -- that is why it survived the cp1252 era intact.
        """
        orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="repo_finding",
            content=self.unicode_content,
        )

        raw = orch.context_file(self.task_id).read_bytes()

        self.assertTrue(raw, "no blackboard bytes written")
        self.assertEqual(
            [byte for byte in raw if byte > 0x7F],
            [],
            "ensure_ascii=True should leave no high bytes on disk",
        )

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
        """The constraint that protects every artifact already in the repo.

        The assertion used to be
        ``assertEqual(raw, raw.decode("ascii").encode("ascii"))``, which is an
        identity on any byte string that decodes at all: it could only fail via
        UnicodeDecodeError, never via the comparison, and it never compared
        against what the pre-change code produced. So it asserted nothing about
        being *unchanged*.

        Both sides now come from different places. The expected line is built
        from the block ``append_context_block`` returns, not from the file's own
        bytes, and it is required to equal what is on disk under ASCII, cp1252
        and UTF-8 alike -- which is the property that makes the encoding change
        safe for every artifact already committed.
        """
        ascii_content = "Plain ASCII decision, no punctuation tricks."

        block = orch.append_context_block(
            self.task_id,
            author="implementer",
            phase="IMPLEMENTING",
            block_type="decision",
            content=ascii_content,
        )

        raw = orch.context_file(self.task_id).read_bytes()

        # Only the trailing newline is translated: the serialised block is one
        # line, and that newline translation is pre-existing text-mode
        # behaviour, separate from the encoding under test.
        expected = (json.dumps(block, sort_keys=True) + "\n").replace(
            "\n", os.linesep
        )

        self.assertEqual(raw, expected.encode("ascii"))
        self.assertEqual(raw, expected.encode("cp1252"))
        self.assertEqual(raw, expected.encode("utf-8"))
        self.assertIn(ascii_content, expected)


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


class AuditDiscriminatesTests(unittest.TestCase):
    """The audit must be able to fail.

    An audit that reports zero offenders is only evidence if it would have
    reported one. TASK-006's implementer checked this by hand against
    `git show HEAD:...` and recorded it in ctx-012; a hand check is not a test,
    so here it is as one. `calls_in_source` takes source text precisely so the
    broken variant never has to be written to disk.
    """

    def test_a_missing_encoding_is_reported(self):
        source = "from pathlib import Path\nPath('x').read_text()\n"

        found = calls_in_source(source)

        self.assertEqual(len(found), 1)
        self.assertFalse(found[0][2], "read_text() has no encoding to find")

    def test_a_present_encoding_is_accepted(self):
        source = "from pathlib import Path\nPath('x').read_text(encoding='utf-8')\n"

        found = calls_in_source(source)

        self.assertTrue(found[0][2])

    def test_stripping_the_encoding_from_a_real_file_creates_an_offender(self):
        """Mutate the real orchestrator source in memory and re-audit it."""
        path = REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"
        source = path.read_text(encoding="utf-8")

        self.assertIn('encoding="utf-8"', source)

        broken = source.replace('encoding="utf-8"', "", 1)
        offenders = [
            entry for entry in calls_in_source(broken, "broken") if not entry[2]
        ]

        self.assertTrue(
            offenders, "removing an encoding must produce an offender"
        )

    def test_the_real_files_have_no_offenders(self):
        """The same logic, unmutated: this is the claim AC-1 rests on."""
        for relative in SCOPED_FILES:
            offenders = [
                entry
                for entry in text_io_calls(REPO_ROOT / relative)
                if not entry[2]
            ]

            self.assertEqual(offenders, [], relative)

    def assert_removing_encoding_flips(self, method, target, mutant):
        """One test's dependence on one production encoding argument.

        A test that "covers" an encoding is evidence only if removing that
        encoding makes it fail. So the covered method runs twice: against the
        orchestrator source as it is, where it must pass, and against a copy
        with exactly one argument cut, where it must not. Re-auditing the
        mutated text would show only that the audit sees the cut; running the
        behavioural method shows the behaviour depended on it.
        """
        source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")

        self.assertEqual(
            source.count(target), 1, "not a unique mutation target: %s" % target
        )

        mutated = source.replace(target, mutant, 1)

        self.assertNotEqual(mutated, source)

        clean = orchestrator_module(source, "clean")
        self.addCleanup(sys.modules.pop, clean.__name__, None)
        broken = orchestrator_module(mutated, "mutant")
        self.addCleanup(sys.modules.pop, broken.__name__, None)

        with omitted_encoding_is_utf16():
            before = run_round_trip(method, clean)
            after = run_round_trip(method, broken)

        self.assertTrue(
            before.wasSuccessful(),
            "%s must pass against the unmutated source: %s"
            % (method, why_it_failed(before)),
        )
        self.assertFalse(
            after.wasSuccessful(),
            "%s passed with %s removed, so it is not evidence about it"
            % (method, target),
        )

    def test_removing_context_write_encoding_fails_the_round_trip_test(self):
        self.assert_removing_encoding_flips(
            "test_context_block_round_trips_unicode",
            CONTEXT_WRITE_CALL,
            CONTEXT_WRITE_MUTANT,
        )

    def test_removing_context_read_encoding_fails_the_round_trip_test(self):
        """The read side fails differently: the block never comes back."""
        self.assert_removing_encoding_flips(
            "test_context_block_round_trips_unicode",
            CONTEXT_READ_CALL,
            CONTEXT_READ_MUTANT,
        )

    def test_removing_context_write_encoding_fails_the_bytes_test(self):
        self.assert_removing_encoding_flips(
            "test_context_file_bytes_are_utf8",
            CONTEXT_WRITE_CALL,
            CONTEXT_WRITE_MUTANT,
        )

    def test_removing_context_write_encoding_fails_the_ascii_compatibility_test(self):
        """Even the ASCII claim has to be encoding-sensitive to be evidence."""
        self.assert_removing_encoding_flips(
            "test_ascii_content_is_byte_for_byte_unchanged",
            CONTEXT_WRITE_CALL,
            CONTEXT_WRITE_MUTANT,
        )


class BinaryModeClassificationTests(unittest.TestCase):
    """AC-4: binary I/O takes no encoding, whichever spelling is used."""

    def _offenders(self, source):
        return [entry for entry in calls_in_source(source) if not entry[2]]

    def test_positional_mode_on_open_is_binary(self):
        """The reported bug: mode is args[1] here, not args[0]."""
        self.assertEqual(self._offenders("open(path, 'rb')\n"), [])

    def test_positional_mode_on_fdopen_is_binary(self):
        self.assertEqual(self._offenders("import os\nos.fdopen(fd, 'rb')\n"), [])

    def test_mode_first_on_path_open_is_binary(self):
        self.assertEqual(self._offenders("p.open('rb')\n"), [])

    def test_keyword_mode_is_binary(self):
        self.assertEqual(self._offenders("open(path, mode='wb')\n"), [])

    def test_a_text_open_without_encoding_is_still_an_offender(self):
        """The fix must not blanket-excuse every open()."""
        self.assertEqual(len(self._offenders("open(path, 'r')\n")), 1)

    def test_os_open_is_not_text_io(self):
        """os.open returns a descriptor; encoding= would be a TypeError."""
        self.assertEqual(
            calls_in_source("import os\nos.open(p, os.O_RDONLY)\n"), []
        )


class AuditCacheTests(unittest.TestCase):
    def test_repeated_audits_return_the_same_result(self):
        path = REPO_ROOT / ".ai" / "hooks" / "stop_guard.py"

        self.assertEqual(text_io_calls(path), text_io_calls(path))

    def test_the_cache_is_populated(self):
        path = REPO_ROOT / ".ai" / "hooks" / "session_start.py"
        text_io_calls(path)

        self.assertIn(str(path), _CALL_CACHE)

    def test_repeated_path_audits_parse_once(self):
        """The cache exists to stop re-parsing, so count the parses.

        Same-result-twice holds for an uncached audit too; only the parse count
        distinguishes them.
        """
        path = REPO_ROOT / ".ai" / "hooks" / "stop_guard.py"
        _CALL_CACHE.pop(str(path), None)
        self.addCleanup(_CALL_CACHE.pop, str(path), None)

        real_parse = ast.parse
        parses = []

        def counting_parse(source, *args, **kwargs):
            parses.append(kwargs.get("filename", "<unknown>"))
            return real_parse(source, *args, **kwargs)

        ast.parse = counting_parse
        self.addCleanup(setattr, ast, "parse", real_parse)

        first = text_io_calls(path)
        second = text_io_calls(path)

        self.assertEqual(first, second)
        self.assertEqual(len(parses), 1, "the second audit re-parsed: %s" % parses)

    def test_calls_in_source_is_not_cached(self):
        """Caching by path must not have leaked into the source-level audit.

        The mutation tests audit several different strings under the same
        default filename; a cache there would hand the second one the first
        one's answer and quietly stop them proving anything.
        """
        with_encoding = calls_in_source(
            "from pathlib import Path\nPath('x').read_text(encoding='utf-8')\n"
        )
        without_encoding = calls_in_source(
            "from pathlib import Path\nPath('x').read_text()\n"
        )

        self.assertTrue(with_encoding[0][2])
        self.assertFalse(without_encoding[0][2])
        self.assertNotEqual(with_encoding, without_encoding)


class Python39CompatibilityTests(unittest.TestCase):
    """AC-5: the CI matrix runs 3.9, so what this task writes must parse there.

    Syntax only. Runtime behaviour on 3.9 and 3.12 is what the matrix itself
    establishes; a single local interpreter cannot stand in for either.
    """

    def test_touched_sources_parse_with_python39_grammar(self):
        for relative in TOUCHED_SOURCES:
            path = REPO_ROOT / relative
            source = path.read_text(encoding="utf-8")

            ast.parse(source, filename=str(path), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
