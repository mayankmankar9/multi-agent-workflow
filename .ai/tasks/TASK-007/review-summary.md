# Review Summary

## Task

TASK-007

## State

VALIDATED

## Branch

None

## Plan Version

5

## Approved Plan

`.ai\tasks\TASK-007\plan-v5.json`

## Diff Base

`origin/main` at `fd35909a6704fef44b6aca33fe9b37d6720b9d96`

Excludes `.ai/tasks/`.

## Files Changed

```text
M	.ai/hooks/session_start.py
M	.ai/hooks/stop_guard.py
A	test_hook_resilience.py
M	test_utf8_io.py
```

## Git Diff

```diff
diff --git a/.ai/hooks/session_start.py b/.ai/hooks/session_start.py
index 708c633..62145a2 100644
--- a/.ai/hooks/session_start.py
+++ b/.ai/hooks/session_start.py
@@ -21,6 +21,58 @@ CONTEXT_FILENAME = "context.jsonl"
 CONSTITUTION = Path(".ai") / "constitution.md"
 
 
+def read_or_warn(path):
+    """Read injected context, saying so on stderr if part of it is unreadable.
+
+    This hook injects rather than blocks, so unlike the Stop guard it must not
+    refuse -- but it must not pretend either. An undecodable byte previously
+    raised out of the hook, and injection was lost silently because a
+    SessionStart failure does not stop the session. Returning what is readable
+    and naming the gap on stderr means the worker starts with less context and
+    somebody can see why.
+    """
+    try:
+        return path.read_text(encoding="utf-8")
+    except UnicodeDecodeError as exc:
+        sys.stderr.write(
+            "session_start: %s is not valid UTF-8 (%s); "
+            "injecting without it.\n" % (path, exc)
+        )
+        return ""
+    except OSError as exc:
+        sys.stderr.write(
+            "session_start: %s could not be read (%s); "
+            "injecting without it.\n" % (path, exc)
+        )
+        return ""
+
+
+def emit(text):
+    """Write injected context without depending on the locale codec.
+
+    ``sys.stdout`` uses the locale encoding, which is cp1252 on Windows. A
+    block containing anything outside it -- the Japanese text TASK-006's own
+    tests write, say -- raised UnicodeEncodeError, the hook died, and the
+    blackboard injection this hook exists to guarantee was gone. That is the
+    same failure mode TASK-006 set out to remove, one layer further out.
+
+    The underlying buffer takes bytes, so encoding here is explicit and the
+    locale never gets a vote. `errors="replace"` on the fallback path keeps a
+    lone unencodable character from costing the whole injection.
+    """
+    data = text.encode("utf-8", errors="replace")
+    buffer = getattr(sys.stdout, "buffer", None)
+
+    if buffer is None:
+        # A replaced stdout (a test harness, a captured stream) may have no
+        # buffer. Fall back to the text layer, lossily rather than not at all.
+        sys.stdout.write(data.decode("utf-8", errors="replace"))
+        return
+
+    buffer.write(data)
+    buffer.flush()
+
+
 def blocks(task_id):
     path = Path(".ai") / "tasks" / task_id / CONTEXT_FILENAME
 
@@ -29,7 +81,7 @@ def blocks(task_id):
 
     found = []
 
-    for line in path.read_text(encoding="utf-8").splitlines():
+    for line in read_or_warn(path).splitlines():
         if not line.strip():
             continue
 
@@ -50,7 +102,7 @@ def main():
     parts = []
 
     if CONSTITUTION.is_file():
-        text = CONSTITUTION.read_text(encoding="utf-8").strip()
+        text = read_or_warn(CONSTITUTION).strip()
 
         if text:
             parts.append("Project invariants (%s):\n\n%s" % (CONSTITUTION, text))
@@ -82,7 +134,7 @@ def main():
     if not parts:
         return 0
 
-    sys.stdout.write("\n\n".join(parts) + "\n")
+    emit("\n\n".join(parts) + "\n")
     return 0
 
 
diff --git a/.ai/hooks/stop_guard.py b/.ai/hooks/stop_guard.py
index 4628a4c..b7c6e18 100644
--- a/.ai/hooks/stop_guard.py
+++ b/.ai/hooks/stop_guard.py
@@ -25,6 +25,34 @@ BLOCK = 2  # the only exit code the harness treats as a refusal
 ALLOW = 0
 
 
+class Unreadable(Exception):
+    """A file the guard must read could not be decoded.
+
+    Raised rather than swallowed so the caller blocks. Reading a
+    worker-writable artifact with a strict codec and no handler meant an
+    undecodable byte killed the script with exit 1 -- and exit 1 blocks
+    nothing, so the write-back guard simply did not run. Switching to UTF-8
+    made that worse, not better: the previous locale decode on Windows rejected
+    five byte values, while UTF-8 rejects any malformed high-byte sequence.
+
+    A guard that cannot read its own input must fail closed.
+    """
+
+
+def read_guarded(path):
+    """Read a file the guard's verdict depends on, or refuse to have a verdict."""
+    try:
+        return path.read_text(encoding="utf-8")
+    except UnicodeDecodeError as exc:
+        raise Unreadable(
+            "%s is not valid UTF-8 (%s). The guard cannot confirm the "
+            "write-back happened, so it refuses rather than assuming it did."
+            % (path, exc)
+        )
+    except OSError as exc:
+        raise Unreadable("%s could not be read (%s)." % (path, exc))
+
+
 def context_blocks(task_id):
     path = Path(".ai") / "tasks" / task_id / CONTEXT_FILENAME
 
@@ -33,7 +61,7 @@ def context_blocks(task_id):
 
     found = []
 
-    for line in path.read_text(encoding="utf-8").splitlines():
+    for line in read_guarded(path).splitlines():
         if not line.strip():
             continue
 
@@ -60,18 +88,24 @@ def main():
 
     notes = task_dir / "implementation.md"
 
-    if not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
-        problems.append(
-            "%s is missing or empty. Write what you changed, any deviation "
-            "from the approved plan, and anything you could not complete."
-            % notes
-        )
-
-    written = [
-        block
-        for block in context_blocks(task_id)
-        if block.get("author") and block["author"] != "orchestrator"
-    ]
+    try:
+        if not notes.is_file() or not read_guarded(notes).strip():
+            problems.append(
+                "%s is missing or empty. Write what you changed, any deviation "
+                "from the approved plan, and anything you could not complete."
+                % notes
+            )
+
+        written = [
+            block
+            for block in context_blocks(task_id)
+            if block.get("author") and block["author"] != "orchestrator"
+        ]
+    except Unreadable as exc:
+        # Block, do not die. Exit 1 here would let the session end with the
+        # guard silently skipped, which is the failure this catches.
+        sys.stderr.write("Session cannot end yet:\n\n- %s\n" % exc)
+        return BLOCK
 
     if not written:
         problems.append(
diff --git a/test_hook_resilience.py b/test_hook_resilience.py
new file mode 100644
index 0000000..e554821
--- /dev/null
+++ b/test_hook_resilience.py
@@ -0,0 +1,310 @@
+"""Hooks must not be defeated by their own input.
+
+TASK-006 made the hooks read with a strict UTF-8 codec. That was right, and it
+introduced two failure modes the reviewers caught:
+
+- `stop_guard.py` read worker-writable artifacts with no handler, so malformed
+  bytes raised, the script exited 1, and **exit 1 blocks nothing** -- the
+  write-back guard silently did not run. The strict codec made this worse than
+  the locale decode it replaced: cp1252 rejects five byte values, UTF-8 rejects
+  any malformed high-byte sequence.
+- `session_start.py` read UTF-8 and then wrote through `sys.stdout`, which uses
+  the locale codec. A block containing anything outside cp1252 raised
+  UnicodeEncodeError and the blackboard injection the hook exists to guarantee
+  was lost -- the same failure mode TASK-006 set out to remove.
+
+A guard fails closed. An injector degrades loudly. Neither dies quietly.
+"""
+
+import contextlib
+import importlib.util
+import io
+import json
+import os
+import subprocess
+import sys
+import unittest
+from pathlib import Path
+
+from _support import REPO_ROOT
+
+HOOKS = REPO_ROOT / ".ai" / "hooks"
+
+
+def load_hook(filename, module_name):
+    """Import a hook so its functions can be called directly.
+
+    ``_support.load_script`` reaches ``.ai/scripts`` only, and some branches
+    cannot be reached from a subprocess at all: a real child process always has
+    a stdout with a ``.buffer``, so the fallback in ``emit`` is unreachable from
+    the tests below that run the hook as the harness does.
+    """
+    path = HOOKS / filename
+    spec = importlib.util.spec_from_file_location(module_name, path)
+
+    if spec is None or spec.loader is None:
+        raise ImportError("Cannot load %s" % path)
+
+    module = importlib.util.module_from_spec(spec)
+    sys.modules[module_name] = module
+    spec.loader.exec_module(module)
+    return module
+
+BLOCK = 2
+ALLOW = 0
+
+# Valid in cp1252, invalid as UTF-8: a lone 0x81 continuation byte.
+INVALID_UTF8 = b"note\x81\x81 broken\n"
+
+# Outside cp1252 entirely, so a locale-encoded stdout cannot emit it.
+NON_CP1252 = "日本語"
+
+
+class HookCase(unittest.TestCase):
+    """Each test gets a throwaway repo-shaped tree."""
+
+    task_id = "TASK-999"
+
+    def setUp(self):
+        import shutil
+        import tempfile
+
+        self.tmp = Path(tempfile.mkdtemp(prefix="wf-hook-"))
+        self.addCleanup(shutil.rmtree, self.tmp, True)
+
+        self.task_path = self.tmp / ".ai" / "tasks" / self.task_id
+        self.task_path.mkdir(parents=True)
+
+    def run_hook(self, script, env=None):
+        """Run a hook as the harness does, capturing raw bytes.
+
+        PYTHONIOENCODING and PYTHONUTF8 are cleared deliberately: either one
+        would make stdout UTF-8 regardless of the code under test and turn the
+        encoding assertions below into tautologies.
+        """
+        environment = dict(os.environ)
+        environment["ORCHESTRATOR_TASK_ID"] = self.task_id
+        environment.pop("PYTHONIOENCODING", None)
+        environment.pop("PYTHONUTF8", None)
+        environment.update(env or {})
+
+        return subprocess.run(
+            [sys.executable, str(HOOKS / script)],
+            input=b"",
+            cwd=str(self.tmp),
+            capture_output=True,
+            env=environment,
+        )
+
+    def write_notes(self, content="Did the thing.\n"):
+        path = self.task_path / "implementation.md"
+
+        if isinstance(content, bytes):
+            path.write_bytes(content)
+        else:
+            path.write_text(content, encoding="utf-8")
+
+        return path
+
+    def write_block(self, content="Learned a thing.", author="implementer"):
+        payload = {
+            "block_id": "ctx-001",
+            "type": "repo_finding",
+            "author": author,
+            "phase": "IMPLEMENTING",
+            "content": content,
+        }
+        path = self.task_path / "context.jsonl"
+        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
+        return path
+
+    def write_raw_block(self, raw):
+        path = self.task_path / "context.jsonl"
+        path.write_bytes(raw)
+        return path
+
+
+class StopGuardFailsClosedTests(HookCase):
+    def test_undecodable_notes_block_the_session(self):
+        """The regression: this used to exit 1, which blocks nothing."""
+        self.write_notes(INVALID_UTF8)
+        self.write_block()
+
+        result = self.run_hook("stop_guard.py")
+
+        self.assertEqual(result.returncode, BLOCK)
+
+    def test_undecodable_notes_say_why(self):
+        self.write_notes(INVALID_UTF8)
+        self.write_block()
+
+        result = self.run_hook("stop_guard.py")
+
+        self.assertIn(b"not valid UTF-8", result.stderr)
+
+    def test_undecodable_blackboard_blocks_the_session(self):
+        self.write_notes()
+        self.write_raw_block(INVALID_UTF8)
+
+        result = self.run_hook("stop_guard.py")
+
+        self.assertEqual(result.returncode, BLOCK)
+
+    def test_it_never_exits_one(self):
+        """Exit 1 is the silent-failure code; a refusal must never use it."""
+        self.write_notes(INVALID_UTF8)
+        self.write_block()
+
+        self.assertNotEqual(self.run_hook("stop_guard.py").returncode, 1)
+
+    def test_a_complete_write_back_still_passes(self):
+        self.write_notes()
+        self.write_block()
+
+        self.assertEqual(self.run_hook("stop_guard.py").returncode, ALLOW)
+
+    def test_a_missing_write_back_still_blocks(self):
+        """The guard's original job must survive the hardening."""
+        self.write_notes()
+
+        self.assertEqual(self.run_hook("stop_guard.py").returncode, BLOCK)
+
+    def test_non_ascii_notes_are_read_not_rejected(self):
+        """Valid UTF-8 outside cp1252 is normal input, not a fault."""
+        self.write_notes("Handled %s correctly.\n" % NON_CP1252)
+        self.write_block()
+
+        self.assertEqual(self.run_hook("stop_guard.py").returncode, ALLOW)
+
+
+class SessionStartEmitsUtf8Tests(HookCase):
+    def test_non_cp1252_content_does_not_crash_the_hook(self):
+        """The regression: sys.stdout would raise UnicodeEncodeError here."""
+        self.write_block(content="Chose %s for the label." % NON_CP1252)
+
+        result = self.run_hook("session_start.py")
+
+        self.assertEqual(result.returncode, 0, result.stderr[:400])
+
+    def test_non_cp1252_content_actually_reaches_stdout(self):
+        """Not crashing is not enough; the injection must be there."""
+        self.write_block(content="Chose %s for the label." % NON_CP1252)
+
+        result = self.run_hook("session_start.py")
+
+        self.assertIn(NON_CP1252.encode("utf-8"), result.stdout)
+
+    def test_stdout_is_utf8_not_the_locale_codec(self):
+        self.write_block(content=NON_CP1252)
+
+        result = self.run_hook("session_start.py")
+        decoded = result.stdout.decode("utf-8")
+
+        self.assertIn(NON_CP1252, decoded)
+        self.assertNotIn("�", decoded)
+
+    def test_ascii_content_is_unaffected(self):
+        self.write_block(content="Plain ASCII finding.")
+
+        result = self.run_hook("session_start.py")
+
+        self.assertEqual(result.returncode, 0)
+        self.assertIn(b"Plain ASCII finding.", result.stdout)
+
+    def test_stdout_without_a_buffer_still_emits(self):
+        """The branch the subprocess tests above cannot reach.
+
+        ``emit`` writes bytes to ``sys.stdout.buffer``, which is what keeps the
+        locale codec out of it. A stdout replaced by an embedding harness -- a
+        captured stream, another agent runtime -- may have no buffer, and that
+        fallback decides whether the injection is written at all or lost to an
+        AttributeError. The claim here is only that the text arrives intact and
+        nothing raises; a caller's own stream can still have a lossy codec of
+        its own, and this cannot speak for that.
+        """
+        module = load_hook("session_start.py", "hook_session_start")
+        captured = io.StringIO()
+
+        self.assertIsNone(
+            getattr(captured, "buffer", None), "premise: this stdout has no buffer"
+        )
+
+        with contextlib.redirect_stdout(captured):
+            module.emit("Chose %s for the label.\n" % NON_CP1252)
+
+        written = captured.getvalue()
+
+        self.assertIn(NON_CP1252, written)
+        self.assertNotIn("�", written)
+
+
+class SessionStartDegradesLoudlyTests(HookCase):
+    def test_an_undecodable_blackboard_does_not_crash(self):
+        """An injector must not block, so it degrades instead."""
+        self.write_raw_block(INVALID_UTF8)
+
+        result = self.run_hook("session_start.py")
+
+        self.assertEqual(result.returncode, 0)
+
+    def test_an_undecodable_blackboard_is_reported(self):
+        """Degrading silently would hide a lost injection."""
+        self.write_raw_block(INVALID_UTF8)
+
+        result = self.run_hook("session_start.py")
+
+        self.assertIn(b"not valid UTF-8", result.stderr)
+
+    def test_it_stays_quiet_outside_a_task(self):
+        """Ordinary sessions in this repo must be unaffected."""
+        result = self.run_hook("session_start.py", env={"ORCHESTRATOR_TASK_ID": ""})
+
+        self.assertEqual(result.returncode, 0)
+        self.assertEqual(result.stdout, b"")
+
+
+class HookExitCodeContractTests(unittest.TestCase):
+    """Both hooks are held to the exit-code rule in their own docstrings."""
+
+    def test_stop_guard_never_returns_one(self):
+        source = (HOOKS / "stop_guard.py").read_text(encoding="utf-8")
+
+        self.assertNotIn("return 1", source)
+
+    def test_session_start_never_returns_two(self):
+        """SessionStart must not block a session from starting."""
+        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")
+
+        self.assertNotIn("return 2", source)
+
+    def test_stop_guard_reads_only_through_the_guarded_helper(self):
+        """A second bare read_text is the defect returning.
+
+        One occurrence is expected and required: the one inside read_guarded.
+        """
+        source = (HOOKS / "stop_guard.py").read_text(encoding="utf-8")
+
+        self.assertEqual(
+            source.count(".read_text("),
+            1,
+            "every guarded read must go through read_guarded",
+        )
+
+    def test_session_start_reads_only_through_its_helper(self):
+        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")
+
+        self.assertEqual(
+            source.count(".read_text("),
+            1,
+            "every injected read must go through read_or_warn",
+        )
+
+    def test_session_start_encodes_its_output_explicitly(self):
+        """Writing the joined parts straight to sys.stdout is the defect."""
+        source = (HOOKS / "session_start.py").read_text(encoding="utf-8")
+
+        self.assertIn('encode("utf-8"', source)
+
+
+if __name__ == "__main__":
+    unittest.main()
diff --git a/test_utf8_io.py b/test_utf8_io.py
index 62d7249..f69c7c1 100644
--- a/test_utf8_io.py
+++ b/test_utf8_io.py
@@ -19,6 +19,11 @@ Two layers, because either alone is insufficient:
 """
 
 import ast
+import contextlib
+import json
+import os
+import sys
+import types
 import unittest
 from pathlib import Path
 
@@ -34,6 +39,11 @@ SCOPED_FILES = (
     ".ai/hooks/stop_guard.py",
 )
 
+# The files TASK-007 changes, held to the oldest interpreter the CI matrix runs.
+TOUCHED_SOURCES = ("test_utf8_io.py", "test_hook_resilience.py")
+
+ORCHESTRATOR_PATH = REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"
+
 # Text stream APIs: each opens or reads a decoded str and so takes an
 # ``encoding``. ``os.open`` is excluded on purpose -- it returns a raw file
 # descriptor, has no encoding to specify, and passing one is a TypeError.
@@ -71,25 +81,66 @@ def is_os_open(node):
 
 
 def is_binary_mode(node):
+    """Whether this call opens a binary stream, which takes no encoding.
+
+    The mode's position varies with the call: ``Path.open("rb")`` puts it
+    first, while ``open(path, "rb")`` and ``os.fdopen(fd, "rb")`` put it
+    second. Inspecting only ``args[0]`` therefore classified both of the
+    latter as *text* I/O and reported them as offenders for lacking an
+    encoding -- a hard failure for legitimate binary I/O, and the exact
+    inverse of the plan's stated risk.
+
+    So every positional argument is considered. A path or descriptor is never
+    spelled ``"rb"``, so scanning them all costs nothing and stops the check
+    depending on which spelling a call happens to use.
+    """
     mode = None
 
     for keyword in node.keywords:
         if keyword.arg == "mode":
             mode = keyword.value
 
-    if mode is None and node.args:
-        # ``open``/``fdopen`` take mode positionally; ``read_text`` does not.
-        first = node.args[0]
-        if isinstance(first, ast.Constant) and isinstance(first.value, str):
-            if first.value in BINARY_MODES:
-                mode = first
+    if mode is None:
+        for argument in node.args:
+            if (
+                isinstance(argument, ast.Constant)
+                and isinstance(argument.value, str)
+                and argument.value in BINARY_MODES
+            ):
+                mode = argument
+                break
 
     return isinstance(mode, ast.Constant) and mode.value in BINARY_MODES
 
 
 def text_io_calls(path):
-    """Every text stream call in one file, as ``(lineno, label, has_encoding)``."""
-    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
+    """Every text stream call in one file, as ``(lineno, label, has_encoding)``.
+
+    Results are cached per path: five tests audit the same four files, and
+    orchestrator.py is ~4000 lines, so the uncached version read and re-parsed
+    the same sources twenty times to reach the same answer.
+    """
+    key = str(path)
+
+    if key in _CALL_CACHE:
+        return _CALL_CACHE[key]
+
+    found = calls_in_source(path.read_text(encoding="utf-8"), str(path))
+    _CALL_CACHE[key] = found
+    return found
+
+
+_CALL_CACHE = {}
+
+
+def calls_in_source(source, filename="<source>"):
+    """The audit itself, over source text rather than a path.
+
+    Split out so the audit can be run against a deliberately broken copy of a
+    file without writing that copy to disk -- which is how
+    ``AuditDiscriminatesTests`` proves the audit can actually fail.
+    """
+    tree = ast.parse(source, filename=filename)
     found = []
 
     for node in ast.walk(tree):
@@ -126,6 +177,123 @@ def utf8_keyword(node):
     return False
 
 
+# The two production calls the blackboard round trip depends on. Each is
+# asserted unique before it is cut, so a mutation lands on the call the test
+# names rather than on something that merely looks like it.
+CONTEXT_WRITE_CALL = 'context_file(task_id).open("a", encoding="utf-8")'
+CONTEXT_WRITE_MUTANT = 'context_file(task_id).open("a")'
+CONTEXT_READ_CALL = 'for line in path.read_text(encoding="utf-8").splitlines():'
+CONTEXT_READ_MUTANT = "for line in path.read_text().splitlines():"
+
+_MUTANT_SERIAL = [0]
+
+
+class UnescapedJson:
+    """``json`` whose ``dump`` defaults to ``ensure_ascii=False``.
+
+    ``append_context_block`` serialises with ``ensure_ascii=True``, so
+    context.jsonl is pure ASCII on disk however it is encoded -- which is why
+    the round-trip tests named below could not fail, and passed with the fix
+    removed. Substituting this for one module's ``json`` puts real multi-byte
+    characters in the file, so the encoding argument becomes the thing under
+    test. Production serialisation is untouched: the substitution is on the
+    module object handed to a single test and is undone when it ends.
+    """
+
+    def __init__(self, real):
+        self._real = real
+
+    def dump(self, obj, fp, **kwargs):
+        kwargs.setdefault("ensure_ascii", False)
+        return self._real.dump(obj, fp, **kwargs)
+
+    def __getattr__(self, name):
+        return getattr(self._real, name)
+
+
+def orchestrator_module(source, label):
+    """Execute orchestrator source as a throwaway module, without writing it.
+
+    The mutation tests need a module whose bytes differ from the file on disk.
+    Exec'ing in a fresh namespace keeps the broken copy off the filesystem --
+    the same reason ``calls_in_source`` takes source text rather than a path --
+    and gives each test its own module, so no mutant can leak into another.
+    """
+    _MUTANT_SERIAL[0] += 1
+    name = "workflow_orchestrator_%s_%d" % (label, _MUTANT_SERIAL[0])
+    module = types.ModuleType(name)
+    module.__file__ = str(ORCHESTRATOR_PATH)
+    sys.modules[name] = module
+
+    try:
+        exec(compile(source, str(ORCHESTRATOR_PATH), "exec"), module.__dict__)
+    except BaseException:
+        sys.modules.pop(name, None)
+        raise
+
+    return module
+
+
+@contextlib.contextmanager
+def omitted_encoding_is_utf16():
+    """Give a call with no ``encoding=`` a deterministic, hostile default.
+
+    Removing ``encoding="utf-8"`` falls back to the locale codec, which on a
+    UTF-8 host *is* UTF-8 -- so a mutant would pass on the Linux runners while
+    failing on Windows, and the mutation test would prove different things on
+    different machines. Pinning the fallback to UTF-16 makes the removed
+    argument observable everywhere.
+
+    Explicit encodings and binary modes are passed straight through, so a
+    failure under this shim can only come from the argument the test removed.
+    """
+    original = Path.open
+
+    def open_with_utf16_default(
+        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
+    ):
+        # 3.10+ resolves an omitted encoding to the sentinel "locale" before
+        # Path.open sees it; 3.9 passes None through.
+        if "b" not in mode and encoding in (None, "locale"):
+            encoding = "utf-16"
+
+        return original(self, mode, buffering, encoding, errors, newline)
+
+    Path.open = open_with_utf16_default
+
+    try:
+        yield
+    finally:
+        Path.open = original
+
+
+def run_round_trip(method, module):
+    """Run one ``Utf8RoundTripTests`` method against a given orchestrator.
+
+    The round-trip tests reach the orchestrator through this module's ``orch``
+    global, so binding a different module there is what points them at a
+    mutant. Running through ``TestCase.run`` keeps setUp, the temp tree and the
+    cleanups exactly as they are in a normal run.
+    """
+    previous = globals()["orch"]
+    globals()["orch"] = module
+    result = unittest.TestResult()
+
+    try:
+        Utf8RoundTripTests(method).run(result)
+    finally:
+        globals()["orch"] = previous
+
+    return result
+
+
+def why_it_failed(result):
+    """The last line of each failure, for an assertion message."""
+    entries = list(result.failures) + list(result.errors)
+
+    return "; ".join(text.strip().splitlines()[-1] for _, text in entries)
+
+
 class Utf8SourceAuditTests(unittest.TestCase):
     """AC-1: every text stream call in the four scoped files declares UTF-8."""
 
@@ -203,7 +371,31 @@ class Utf8RoundTripTests(TaskDirCase):
         % ("wide", CURLY_QUOTE, "”", NON_LATIN)
     )
 
+    def write_real_high_bytes(self):
+        """Make this test's blackboard writes land as non-ASCII bytes.
+
+        Scoped to the module the test is actually running against, which is a
+        mutant when ``AuditDiscriminatesTests`` is driving, and undone when the
+        test ends however it ends.
+        """
+        module = orch
+        real = module.json
+        module.json = UnescapedJson(real)
+        self.addCleanup(setattr, module, "json", real)
+
     def test_context_block_round_trips_unicode(self):
+        """AC-2's behavioural claim, over bytes that can actually break.
+
+        Restored under its original name after TASK-007 found the version it
+        replaced could not fail: with ensure_ascii=True the file is pure ASCII,
+        so the round trip held with encoding= removed on every host. The
+        fixture now writes genuine multi-byte characters, so both the write
+        encoding and the read encoding have to be right for the block to come
+        back -- and AuditDiscriminatesTests removes each in turn and requires
+        this test to fail.
+        """
+        self.write_real_high_bytes()
+
         orch.append_context_block(
             self.task_id,
             author="implementer",
@@ -217,10 +409,25 @@ class Utf8RoundTripTests(TaskDirCase):
         self.assertEqual(len(blocks), 1)
         self.assertEqual(blocks[0]["content"], self.unicode_content)
         self.assertNotIn(REPLACEMENT, blocks[0]["content"])
-        self.assertNotIn("?", blocks[0]["content"])
+
+        # The premise the test rests on: without high bytes on disk, the round
+        # trip would hold whatever encoding produced them.
+        raw = orch.context_file(self.task_id).read_bytes()
+
+        self.assertTrue(
+            any(byte > 0x7F for byte in raw), "fixture wrote no non-ASCII bytes"
+        )
 
     def test_context_file_bytes_are_utf8(self):
-        """Read the bytes back, so a lenient locale cannot mask the encoding."""
+        """The bytes on disk are UTF-8, asserted where that can be false.
+
+        Also restored: the version this replaces decoded pure-ASCII bytes as
+        UTF-8, which cannot fail. With the fixture forcing real high bytes, the
+        decode and the two membership checks are all claims about the write
+        encoding.
+        """
+        self.write_real_high_bytes()
+
         orch.append_context_block(
             self.task_id,
             author="implementer",
@@ -231,12 +438,63 @@ class Utf8RoundTripTests(TaskDirCase):
 
         raw = orch.context_file(self.task_id).read_bytes()
 
-        # json.dump escapes non-ASCII by default, which is what keeps existing
-        # JSONL artifacts pure ASCII. Decoding as UTF-8 must still work, and
-        # the decoded text must not have lost anything.
-        decoded = raw.decode("utf-8")
+        self.assertIn(NON_LATIN.encode("utf-8"), raw)
+        self.assertIn(CURLY_QUOTE.encode("utf-8"), raw)
+        self.assertNotIn(REPLACEMENT.encode("utf-8"), raw)
+        self.assertIn(self.unicode_content, raw.decode("utf-8"))
+
+    def test_context_block_survives_json_escaping(self):
+        """JSON-escape fidelity, which is *not* the encoding claim.
+
+        Split off from test_context_block_round_trips_unicode, which read as
+        encoding coverage and was not: append_context_block serialises through
+        json.dumps with the default ensure_ascii=True, so the bytes on disk are
+        pure ASCII whatever encoding= is passed, and this passed with the fix
+        removed on any host.
+
+        The behaviour is still worth pinning -- ensure_ascii round-tripping is
+        what keeps existing JSONL artifacts readable -- so it stays here, under
+        a name that claims only what it checks, while the encoding claim lives
+        in the restored test above.
+        """
+        orch.append_context_block(
+            self.task_id,
+            author="implementer",
+            phase="IMPLEMENTING",
+            block_type="decision",
+            content=self.unicode_content,
+        )
+
+        blocks = orch.read_context(self.task_id)
+
+        self.assertEqual(len(blocks), 1)
+        self.assertEqual(blocks[0]["content"], self.unicode_content)
+        self.assertNotIn(REPLACEMENT, blocks[0]["content"])
+
+    def test_context_file_is_pure_ascii_by_json_escaping(self):
+        """Also not the encoding claim; states the real invariant instead.
 
-        self.assertNotIn(REPLACEMENT, decoded)
+        Split off from test_context_file_bytes_are_utf8, whose UTF-8 decode of
+        pure-ASCII bytes could not fail. What is actually true, and worth
+        holding, is that the blackboard stays ASCII on disk under production
+        serialisation -- that is why it survived the cp1252 era intact.
+        """
+        orch.append_context_block(
+            self.task_id,
+            author="implementer",
+            phase="IMPLEMENTING",
+            block_type="repo_finding",
+            content=self.unicode_content,
+        )
+
+        raw = orch.context_file(self.task_id).read_bytes()
+
+        self.assertTrue(raw, "no blackboard bytes written")
+        self.assertEqual(
+            [byte for byte in raw if byte > 0x7F],
+            [],
+            "ensure_ascii=True should leave no high bytes on disk",
+        )
 
     def test_requirement_text_round_trips_through_task_creation(self):
         requirement = "Support %s names and an em-dash %s here.\n" % (
@@ -263,10 +521,24 @@ class Utf8RoundTripTests(TaskDirCase):
         self.assertIn(NON_LATIN.encode("utf-8"), raw)
 
     def test_ascii_content_is_byte_for_byte_unchanged(self):
-        """The constraint that protects every artifact already in the repo."""
+        """The constraint that protects every artifact already in the repo.
+
+        The assertion used to be
+        ``assertEqual(raw, raw.decode("ascii").encode("ascii"))``, which is an
+        identity on any byte string that decodes at all: it could only fail via
+        UnicodeDecodeError, never via the comparison, and it never compared
+        against what the pre-change code produced. So it asserted nothing about
+        being *unchanged*.
+
+        Both sides now come from different places. The expected line is built
+        from the block ``append_context_block`` returns, not from the file's own
+        bytes, and it is required to equal what is on disk under ASCII, cp1252
+        and UTF-8 alike -- which is the property that makes the encoding change
+        safe for every artifact already committed.
+        """
         ascii_content = "Plain ASCII decision, no punctuation tricks."
 
-        orch.append_context_block(
+        block = orch.append_context_block(
             self.task_id,
             author="implementer",
             phase="IMPLEMENTING",
@@ -276,7 +548,17 @@ class Utf8RoundTripTests(TaskDirCase):
 
         raw = orch.context_file(self.task_id).read_bytes()
 
-        self.assertEqual(raw, raw.decode("ascii").encode("ascii"))
+        # Only the trailing newline is translated: the serialised block is one
+        # line, and that newline translation is pre-existing text-mode
+        # behaviour, separate from the encoding under test.
+        expected = (json.dumps(block, sort_keys=True) + "\n").replace(
+            "\n", os.linesep
+        )
+
+        self.assertEqual(raw, expected.encode("ascii"))
+        self.assertEqual(raw, expected.encode("cp1252"))
+        self.assertEqual(raw, expected.encode("utf-8"))
+        self.assertIn(ascii_content, expected)
 
 
 class Utf8AdrTests(TaskDirCase):
@@ -318,5 +600,230 @@ class Utf8AdrTests(TaskDirCase):
         self.assertIn(NON_LATIN, record.read_text(encoding="utf-8"))
 
 
+class AuditDiscriminatesTests(unittest.TestCase):
+    """The audit must be able to fail.
+
+    An audit that reports zero offenders is only evidence if it would have
+    reported one. TASK-006's implementer checked this by hand against
+    `git show HEAD:...` and recorded it in ctx-012; a hand check is not a test,
+    so here it is as one. `calls_in_source` takes source text precisely so the
+    broken variant never has to be written to disk.
+    """
+
+    def test_a_missing_encoding_is_reported(self):
+        source = "from pathlib import Path\nPath('x').read_text()\n"
+
+        found = calls_in_source(source)
+
+        self.assertEqual(len(found), 1)
+        self.assertFalse(found[0][2], "read_text() has no encoding to find")
+
+    def test_a_present_encoding_is_accepted(self):
+        source = "from pathlib import Path\nPath('x').read_text(encoding='utf-8')\n"
+
+        found = calls_in_source(source)
+
+        self.assertTrue(found[0][2])
+
+    def test_stripping_the_encoding_from_a_real_file_creates_an_offender(self):
+        """Mutate the real orchestrator source in memory and re-audit it."""
+        path = REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"
+        source = path.read_text(encoding="utf-8")
+
+        self.assertIn('encoding="utf-8"', source)
+
+        broken = source.replace('encoding="utf-8"', "", 1)
+        offenders = [
+            entry for entry in calls_in_source(broken, "broken") if not entry[2]
+        ]
+
+        self.assertTrue(
+            offenders, "removing an encoding must produce an offender"
+        )
+
+    def test_the_real_files_have_no_offenders(self):
+        """The same logic, unmutated: this is the claim AC-1 rests on."""
+        for relative in SCOPED_FILES:
+            offenders = [
+                entry
+                for entry in text_io_calls(REPO_ROOT / relative)
+                if not entry[2]
+            ]
+
+            self.assertEqual(offenders, [], relative)
+
+    def assert_removing_encoding_flips(self, method, target, mutant):
+        """One test's dependence on one production encoding argument.
+
+        A test that "covers" an encoding is evidence only if removing that
+        encoding makes it fail. So the covered method runs twice: against the
+        orchestrator source as it is, where it must pass, and against a copy
+        with exactly one argument cut, where it must not. Re-auditing the
+        mutated text would show only that the audit sees the cut; running the
+        behavioural method shows the behaviour depended on it.
+        """
+        source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
+
+        self.assertEqual(
+            source.count(target), 1, "not a unique mutation target: %s" % target
+        )
+
+        mutated = source.replace(target, mutant, 1)
+
+        self.assertNotEqual(mutated, source)
+
+        clean = orchestrator_module(source, "clean")
+        self.addCleanup(sys.modules.pop, clean.__name__, None)
+        broken = orchestrator_module(mutated, "mutant")
+        self.addCleanup(sys.modules.pop, broken.__name__, None)
+
+        with omitted_encoding_is_utf16():
+            before = run_round_trip(method, clean)
+            after = run_round_trip(method, broken)
+
+        self.assertTrue(
+            before.wasSuccessful(),
+            "%s must pass against the unmutated source: %s"
+            % (method, why_it_failed(before)),
+        )
+        self.assertFalse(
+            after.wasSuccessful(),
+            "%s passed with %s removed, so it is not evidence about it"
+            % (method, target),
+        )
+
+    def test_removing_context_write_encoding_fails_the_round_trip_test(self):
+        self.assert_removing_encoding_flips(
+            "test_context_block_round_trips_unicode",
+            CONTEXT_WRITE_CALL,
+            CONTEXT_WRITE_MUTANT,
+        )
+
+    def test_removing_context_read_encoding_fails_the_round_trip_test(self):
+        """The read side fails differently: the block never comes back."""
+        self.assert_removing_encoding_flips(
+            "test_context_block_round_trips_unicode",
+            CONTEXT_READ_CALL,
+            CONTEXT_READ_MUTANT,
+        )
+
+    def test_removing_context_write_encoding_fails_the_bytes_test(self):
+        self.assert_removing_encoding_flips(
+            "test_context_file_bytes_are_utf8",
+            CONTEXT_WRITE_CALL,
+            CONTEXT_WRITE_MUTANT,
+        )
+
+    def test_removing_context_write_encoding_fails_the_ascii_compatibility_test(self):
+        """Even the ASCII claim has to be encoding-sensitive to be evidence."""
+        self.assert_removing_encoding_flips(
+            "test_ascii_content_is_byte_for_byte_unchanged",
+            CONTEXT_WRITE_CALL,
+            CONTEXT_WRITE_MUTANT,
+        )
+
+
+class BinaryModeClassificationTests(unittest.TestCase):
+    """AC-4: binary I/O takes no encoding, whichever spelling is used."""
+
+    def _offenders(self, source):
+        return [entry for entry in calls_in_source(source) if not entry[2]]
+
+    def test_positional_mode_on_open_is_binary(self):
+        """The reported bug: mode is args[1] here, not args[0]."""
+        self.assertEqual(self._offenders("open(path, 'rb')\n"), [])
+
+    def test_positional_mode_on_fdopen_is_binary(self):
+        self.assertEqual(self._offenders("import os\nos.fdopen(fd, 'rb')\n"), [])
+
+    def test_mode_first_on_path_open_is_binary(self):
+        self.assertEqual(self._offenders("p.open('rb')\n"), [])
+
+    def test_keyword_mode_is_binary(self):
+        self.assertEqual(self._offenders("open(path, mode='wb')\n"), [])
+
+    def test_a_text_open_without_encoding_is_still_an_offender(self):
+        """The fix must not blanket-excuse every open()."""
+        self.assertEqual(len(self._offenders("open(path, 'r')\n")), 1)
+
+    def test_os_open_is_not_text_io(self):
+        """os.open returns a descriptor; encoding= would be a TypeError."""
+        self.assertEqual(
+            calls_in_source("import os\nos.open(p, os.O_RDONLY)\n"), []
+        )
+
+
+class AuditCacheTests(unittest.TestCase):
+    def test_repeated_audits_return_the_same_result(self):
+        path = REPO_ROOT / ".ai" / "hooks" / "stop_guard.py"
+
+        self.assertEqual(text_io_calls(path), text_io_calls(path))
+
+    def test_the_cache_is_populated(self):
+        path = REPO_ROOT / ".ai" / "hooks" / "session_start.py"
+        text_io_calls(path)
+
+        self.assertIn(str(path), _CALL_CACHE)
+
+    def test_repeated_path_audits_parse_once(self):
+        """The cache exists to stop re-parsing, so count the parses.
+
+        Same-result-twice holds for an uncached audit too; only the parse count
+        distinguishes them.
+        """
+        path = REPO_ROOT / ".ai" / "hooks" / "stop_guard.py"
+        _CALL_CACHE.pop(str(path), None)
+        self.addCleanup(_CALL_CACHE.pop, str(path), None)
+
+        real_parse = ast.parse
+        parses = []
+
+        def counting_parse(source, *args, **kwargs):
+            parses.append(kwargs.get("filename", "<unknown>"))
+            return real_parse(source, *args, **kwargs)
+
+        ast.parse = counting_parse
+        self.addCleanup(setattr, ast, "parse", real_parse)
+
+        first = text_io_calls(path)
+        second = text_io_calls(path)
+
+        self.assertEqual(first, second)
+        self.assertEqual(len(parses), 1, "the second audit re-parsed: %s" % parses)
+
+    def test_calls_in_source_is_not_cached(self):
+        """Caching by path must not have leaked into the source-level audit.
+
+        The mutation tests audit several different strings under the same
+        default filename; a cache there would hand the second one the first
+        one's answer and quietly stop them proving anything.
+        """
+        with_encoding = calls_in_source(
+            "from pathlib import Path\nPath('x').read_text(encoding='utf-8')\n"
+        )
+        without_encoding = calls_in_source(
+            "from pathlib import Path\nPath('x').read_text()\n"
+        )
+
+        self.assertTrue(with_encoding[0][2])
+        self.assertFalse(without_encoding[0][2])
+        self.assertNotEqual(with_encoding, without_encoding)
+
+
+class Python39CompatibilityTests(unittest.TestCase):
+    """AC-5: the CI matrix runs 3.9, so what this task writes must parse there.
+
+    Syntax only. Runtime behaviour on 3.9 and 3.12 is what the matrix itself
+    establishes; a single local interpreter cannot stand in for either.
+    """
+
+    def test_touched_sources_parse_with_python39_grammar(self):
+        for relative in TOUCHED_SOURCES:
+            path = REPO_ROOT / relative
+            source = path.read_text(encoding="utf-8")
+
+            ast.parse(source, filename=str(path), feature_version=(3, 9))
+
+
 if __name__ == "__main__":
     unittest.main()
```

## Validation

- Command: `C:\Users\manka\AppData\Local\Programs\Python\Python313\python.exe -m unittest -v`
- Return code: 0
- Tests run: 700
- Result: PASSED

## Acceptance Criteria

| Criterion | Result | Verify |
|---|---|---|
| AC-1 As a deliberate regression guard over already-landed hook code, malformed UTF-8 in either Stop input exits 2 with a reason on stderr, and the existing completed and incomplete write-back contracts remain intact. | PASS | `{python} -m unittest -v test_hook_resilience.StopGuardFailsClosedTests test_hook_resilience.HookExitCodeContractTests` |
| AC-2 SessionStart continues to emit Japanese text as UTF-8 and exit 0 through the normal subprocess path, and the newly covered stdout-without-buffer fallback emits the same text without raising or losing it. | PASS | `{python} -m unittest -v test_hook_resilience.SessionStartEmitsUtf8Tests.test_non_cp1252_content_does_not_crash_the_hook test_hook_resilience.SessionStartEmitsUtf8Tests.test_non_cp1252_content_actually_reaches_stdout test_hook_resilience.SessionStartEmitsUtf8Tests.test_stdout_is_utf8_not_the_locale_codec test_hook_resilience.SessionStartEmitsUtf8Tests.test_stdout_without_a_buffer_still_emits test_hook_resilience.SessionStartDegradesLoudlyTests` |
| AC-3 The restored F4 tests and strengthened F5 test are encoding-sensitive, and every named mutation test runs its specific Utf8RoundTripTests method against both the unmodified and uniquely mutated in-memory orchestrator modules and asserts a pass-then-fail transition when the relevant encoding argument is removed. | PASS | `{python} -m unittest -v test_utf8_io.Utf8RoundTripTests.test_context_block_round_trips_unicode test_utf8_io.Utf8RoundTripTests.test_context_file_bytes_are_utf8 test_utf8_io.Utf8RoundTripTests.test_ascii_content_is_byte_for_byte_unchanged test_utf8_io.AuditDiscriminatesTests.test_removing_context_write_encoding_fails_the_round_trip_test test_utf8_io.AuditDiscriminatesTests.test_removing_context_read_encoding_fails_the_round_trip_test test_utf8_io.AuditDiscriminatesTests.test_removing_context_write_encoding_fails_the_bytes_test test_utf8_io.AuditDiscriminatesTests.test_removing_context_write_encoding_fails_the_ascii_compatibility_test` |
| AC-4 Binary positional and keyword modes remain correctly classified, repeated path audits invoke ast.parse exactly once, and calls_in_source remains uncached so different in-memory source mutations are independently parsed. | PASS | `{python} -m unittest -v test_utf8_io.BinaryModeClassificationTests test_utf8_io.AuditCacheTests.test_repeated_path_audits_parse_once test_utf8_io.AuditCacheTests.test_calls_in_source_is_not_cached` |
| AC-5 Both files changed by this contract parse with Python 3.9 grammar, and the complete repository unittest suite is non-empty and passes under the orchestrator-provided validation interpreter. | PASS | `{python} -m unittest -v test_utf8_io.Python39CompatibilityTests.test_touched_sources_parse_with_python39_grammar && {python} -c "import unittest; s=unittest.defaultTestLoader.discover('.'); n=s.countTestCases(); print('discovered %d tests' % n); r=unittest.TextTestRunner(verbosity=2).run(s); raise SystemExit(0 if n > 0 and r.wasSuccessful() else 1)"` |
| AC-6 Full runtime execution on Python 3.9 and 3.12 is explicitly delegated to the unchanged CI matrix and must be confirmed by CI outside local acceptance validation; this command verifies only that both required versions remain configured in that matrix. | PASS | `{python} -c "from pathlib import Path; s=Path('.github/workflows/ci.yml').read_text(encoding='utf-8'); assert 'python-version: [\"3.9\", \"3.12\"]' in s and 'run: python -m unittest -v' in s"` |

## Diff Scope

- Declared in the plan: test_hook_resilience.py, test_utf8_io.py
- No undeclared files changed.

## Reviewer Findings

- **correctness**: 6 finding(s). Examined: Requirement TASK-007/requirement.md (F1-F6, AC-1..AC-5) and plan-v5.json requirements/acceptance_criteria/constraints, read as the contract for this change; implementation.md, including the fix-run note claiming no source edits and the two-file delta vs baseline; `.ai/hooks/stop_guard.py` in full: `Unreadable`, `read_guarded`, `context_blocks`, `main` exit-code paths (ALLOW/BLOCK, never 1) and the short-circuit around a missing implementation.md; `.ai/hooks/session_start.py` in full: `read_or_warn` degradation, `emit` buffer path and no-buffer fallback, `blocks`, `main`; `test_hook_resilience.py` in full: `load_hook`, `HookCase.run_hook` environment handling, StopGuardFailsClosedTests, SessionStartEmitsUtf8Tests (incl. the new `test_stdout_without_a_buffer_still_emits`), SessionStartDegradesLoudlyTests, HookExitCodeContractTests; `test_utf8_io.py` in full: `is_binary_mode` positional/keyword scan, `calls_in_source`/`text_io_calls` and `_CALL_CACHE`, `UnescapedJson`, `orchestrator_module`, `omitted_encoding_is_utf16`, `run_round_trip`, the restored F4 tests, the strengthened F5 test, the four mutation regressions, BinaryModeClassificationTests, AuditCacheTests, Python39CompatibilityTests; Cross-checked the mutation targets against the real production call sites: `orchestrator.py:918` (`context_file(task_id).open("a", encoding="utf-8")`, `json.dump(..., sort_keys=True)`) and `orchestrator.py:865` (`for line in path.read_text(encoding="utf-8").splitlines():`), including uniqueness vs the similar line at 961; `_support.py` `TaskDirCase.setUp`/cleanups, since the mutation harness re-runs those tests nested; `.github/workflows/ci.yml` against the literal strings AC-6's verify command greps for, and the runner OS/matrix; Python 3.9 vs 3.12 signatures relied on by the `Path.open` shim (`mode, buffering, encoding, errors, newline`) and by `Path.read_text`'s `io.text_encoding` sentinel on 3.10+; Existing callers of the hooks in `test_context_bus.py` for behaviour the hardening could have broken; Note: I have no Bash tool, so I did not execute git or the test suite; this review is of the working-tree contents of the files above..
  - `medium` The AC-2 stdout-encoding regression guard cannot fail on the only platform CI runs — test_hook_resilience.py:78 (AC-2)
    `run_hook` deliberately strips PYTHONIOENCODING and PYTHONUTF8 so the child's stdout uses the locale codec. That makes `test_stdout_is_utf8_not_the_locale_codec`, `test_non_cp1252_content_actually_reaches_stdout` and `test_non_cp1252_content_does_not_crash_the_hook` meaningful on the developer's Windows box (cp1252) but tautological on ubuntu-latest, whose locale codec is already UTF-8: the pre-fix `sys.stdout.write(...)` implementation passes all three there. `.github/workflows/ci.yml:14` pins `runs-on: ubuntu-latest` for both matrix entries, so reverting `emit()` to a plain `sys.stdout.write` would be caught by nothing in CI except the source-text grep in `test_session_start_encodes_its_output_explicitly`. Requirement AC-2 states the claim explicitly as "on Windows". A platform-independent version is available without touching ci.yml: set `PYTHONIOENCODING=cp1252` (rather than clearing it) for one dedicated test, which makes the old code raise UnicodeEncodeError on any host.
  - `low` Mutation harness asserts the mutant failed, but not that it failed for the encoding reason — test_utf8_io.py:689 (AC-3)
    `assert_removing_encoding_flips` accepts any non-successful `TestResult` from the mutated module. `why_it_failed` is computed only for the pass-side message, so a mutant that errors for an unrelated reason (an exec-time difference, a setUp problem, a temp-dir failure) satisfies `assertFalse(after.wasSuccessful())` and the test still reports the encoding as covered. The uniqueness assertion on the mutation target and the clean-run pass reduce but do not remove this: asserting that the failure text mentions the codec/decode error (or that the failure is a UnicodeDecodeError/byte mismatch) would close it.
  - `low` INVALID_UTF8's comment misstates cp1252, undercutting the fixture's stated purpose — test_hook_resilience.py:56 (AC-1)
    The fixture is annotated "Valid in cp1252: a lone 0x81 continuation byte", but 0x81 is one of exactly five bytes cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) and it raises UnicodeDecodeError under cp1252 too. The module docstring and the requirement build their argument on UTF-8 rejecting byte sequences cp1252 accepts, so this fixture demonstrates the narrow overlap rather than the widened surface it is described as covering. The assertions (exit 2, "not valid UTF-8" on stderr) remain valid either way; only the fixture's claim is wrong. A byte such as 0x92 (valid cp1252 U+2019, invalid UTF-8) would match the comment.
  - `low` emit()'s no-buffer fallback can still lose injection, and the test cannot detect it — .ai/hooks/session_start.py:69 (AC-2)
    When `sys.stdout` has no `.buffer`, `emit` writes a str to the text layer. If that replaced stream carries a lossy codec (the realistic Windows case: a cp1252 text stream), `sys.stdout.write` raises UnicodeEncodeError, the hook dies and the injection is lost - the same F3 failure mode, one branch over. `test_stdout_without_a_buffer_still_emits` exercises the branch with `io.StringIO`, which encodes nothing and accepts any character, so it proves only that the branch exists and does not raise AttributeError. Plan v5 acknowledges the limitation in its risks, so this is a residual gap rather than a plan deviation; a `try/except UnicodeEncodeError` re-write through `sys.stdout.encoding` with `errors="replace"` would make the fallback degrade instead of dying.
  - `low` Two exit-code/encoding contracts are asserted by grepping source text, not by behaviour — test_hook_resilience.py:269 (AC-1, AC-2)
    `test_stop_guard_never_returns_one` asserts the literal string "return 1" is absent from stop_guard.py, and `test_session_start_encodes_its_output_explicitly` asserts 'encode("utf-8"' is present in session_start.py. Neither can fail for the defect class it names: F1 was an exit 1 produced by an *unhandled exception*, not by a `return 1` statement, and the encoding grep passes even if `emit` is never called or its result discarded. The behavioural subprocess tests in the same file carry the real signal; these two read as coverage of the exit-code contract while asserting the implementation's text.
  - `low` Requirement AC-3's "no test in test_utf8_io.py" is proven only for four named methods — test_utf8_io.py:499 (AC-3)
    Plan v5 narrows AC-3 to four mutation regressions and those are correctly built. Against the broader requirement wording, `test_requirement_text_round_trips_through_task_creation` remains unguarded: it depends on `create_task`'s `write_text(..., encoding="utf-8")` at orchestrator.py:4584, and with that argument removed it still passes on any UTF-8-locale host - which is where CI runs. The restored tests' docstrings point at this test as the place "the encoding claim" actually lives, so its lack of a mutation guard matters more than it otherwise would. The existing `omitted_encoding_is_utf16` harness would extend to it directly.
- **performance**: 3 finding(s). Examined: test_utf8_io.py in full: the new _CALL_CACHE path cache in text_io_calls, calls_in_source split, is_binary_mode positional scan, the AuditDiscriminatesTests mutation harness (orchestrator_module / omitted_encoding_is_utf16 / run_round_trip), AuditCacheTests parse-count regressions, Python39CompatibilityTests; test_hook_resilience.py in full: the HookCase subprocess harness (run_hook), the fixture writers, StopGuardFailsClosedTests, SessionStartEmitsUtf8Tests (including the new no-buffer emit test), SessionStartDegradesLoudlyTests, HookExitCodeContractTests source reads; ./.ai/hooks/session_start.py: read_or_warn, emit (encode + buffer write + text fallback), blocks(), main() string assembly; ./.ai/hooks/stop_guard.py: read_guarded, context_blocks, main() control flow and per-run file reads; Plan v5 acceptance criteria AC-1..AC-6 and their verify commands, for what work each test run implies.
  - `low` Mutation harness recompiles and re-executes the ~4000-line orchestrator module 8 times per suite run — test_utf8_io.py:675 (AC-3)
    assert_removing_encoding_flips reads ORCHESTRATOR_PATH and builds two throwaway modules via orchestrator_module(), each of which compile()s and exec()s the entire orchestrator source. It is called by four tests, so a full run performs 4 file reads, 8 compiles and 8 module executions of the largest file in the repo. The 'clean' module is byte-identical across all four calls and could be built once per class (setUpClass) and reused, since each test only needs its own mutant; the mutation targets also differ only in one call, so only 2 distinct mutated sources exist. This is per-test work inside a loop over tests that could be hoisted. Cost is test-suite time only, not production, and it is bounded and deterministic, so it does not block.
  - `low` Two audit tests bypass the new per-path parse cache and re-read/re-parse the scoped files — test_utf8_io.py:327 (AC-4)
    text_io_calls() was given _CALL_CACHE specifically because the same four sources (one ~4000 lines) were parsed repeatedly. test_every_encoding_is_utf8 (line 327) and test_low_level_os_open_is_not_given_an_encoding (line 356) each call path.read_text() plus ast.parse() directly, duplicating the walk logic in calls_in_source rather than reusing the cached result. That reintroduces two full reads+parses of orchestrator.py per run and means the cache's benefit is partial. AuditCacheTests.test_repeated_path_audits_parse_once proves the cached path parses once, but does not cover these two call sites.
  - `low` run_hook spawns a Python subprocess per test with no timeout — test_hook_resilience.py:91 (AC-1, AC-2)
    HookCase.run_hook calls subprocess.run([...]) with capture_output and no timeout= argument. It is the driver for roughly a dozen tests across StopGuardFailsClosedTests, SessionStartEmitsUtf8Tests and SessionStartDegradesLoudlyTests, all of which run under AC-1 and AC-2 and in CI. If a hook ever fails to terminate, the run blocks indefinitely rather than failing; input=b'' closes stdin, which makes the common stdin-hang case unlikely, so this is a resilience gap rather than an observed stall. Every subprocess invocation in .ai/scripts/orchestrator.py carries an explicit *_TIMEOUT_S, so the repo convention is otherwise consistent.
- **security**: 1 finding(s). Examined: .ai/hooks/stop_guard.py in full (Unreadable/read_guarded fail-closed path, exit-code handling, env-driven task path construction, stderr message content); .ai/hooks/session_start.py in full (read_or_warn degradation, emit() explicit UTF-8 encoding and no-buffer text fallback, blackboard/constitution assembly into stdout); test_hook_resilience.py in full (subprocess invocation shape, env handling, temp-tree fixtures, direct hook import via importlib); test_utf8_io.py in full (calls_in_source/text_io_calls AST audit, _CALL_CACHE, orchestrator_module exec of orchestrator source, omitted_encoding_is_utf16 Path.open shim and its restoration, ast.parse counter and its restoration, mutation targets); grep across all *.py for shell=True, os.system, eval(, exec( to locate command-construction surfaces in and around the changed files; grep for ORCHESTRATOR_SKIP_STOP_GUARD to see where the guard bypass is set (only .ai/hooks/stop_guard.py and test_context_bus.py, the latter outside this change); module-level side-effect scan of .ai/scripts/orchestrator.py (mkdir/write_text/open/subprocess/environ at import scope) because test_utf8_io.orchestrator_module exec()s that source in-process — none found; .ai/tasks/TASK-007/requirement.md, plan-v5.json and implementation.md for declared scope and constraints.
  - `low` SessionStart now reliably injects arbitrary repository text into a write-capable worker's context — .ai/hooks/session_start.py:63 (AC-2)
    emit() (session_start.py:50-73) replaces locale-encoded stdout with an explicit UTF-8 byte write, plus a lossy text fallback. That is the right fix for the lost-injection bug, but it also means blackboard block content and .ai/constitution.md text that previously killed the hook (UnicodeEncodeError on non-cp1252 characters) is now delivered verbatim into the prompt of workers that hold Edit/Write/Bash. The delivered payload can now include full non-ASCII content — homoglyphs, bidi controls, zero-width characters — assembled from files any contributor can modify (main(), session_start.py:110-137). The only mitigation is the trailing advisory sentence at session_start.py:128-131, and it is appended to the blocks section only; the constitution section (session_start.py:104-108) is emitted with no data-not-instructions framing. No sanitisation or control-character stripping is applied on the way out. Impact is bounded because the repo's own files are the source and the framing sentence exists, so this is a widened surface rather than a demonstrated exploit.

## Evidence

- Implementation record: implementation.md
- Validation record: validation.md
- Machine-readable tests: test-results.json
- Event log: events.jsonl
- Reviewer findings: review-findings.json
