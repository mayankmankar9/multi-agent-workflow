# TASK-007 — Act on the TASK-006 reviewer findings

Six findings came out of TASK-006's review. None was blocking; all are in code
TASK-006 touched. Full text in `.ai/tasks/TASK-006/review-findings.json`.

## Problem

Three of the six matter beyond tidiness.

**The encoding fix widened a fail-open path.** `stop_guard.py` now reads
worker-writable artifacts with a strict UTF-8 codec and no `try/except`.
Malformed bytes raise `UnicodeDecodeError`, the script dies with exit 1, and
only exit 2 blocks — so the write-back guard never runs. Under the previous
locale decode on Windows only five byte values were undecodable; UTF-8 rejects
any malformed high-byte sequence, so the change made the guard *easier* to
bypass than before. `session_start.py` has the same shape.

**Blackboard injection can still be lost to encoding.** `session_start.py`
reads `context.jsonl` as UTF-8 and then writes it through `sys.stdout`, which
uses the locale codec on Windows. A block containing a character outside cp1252
raises `UnicodeEncodeError` and the injection the hook exists to guarantee is
gone. That is the same failure mode TASK-006 set out to remove, in a file
TASK-006 had in scope.

**Two acceptance-criterion tests cannot fail.**
`test_context_block_round_trips_unicode` and `test_context_file_bytes_are_utf8`
serialise through `json.dumps` with the default `ensure_ascii=True`, so the
bytes on disk are pure ASCII regardless of `encoding=`. Both pass with the fix
removed. They contribute no signal to AC-2 while reading as coverage.

## Scope

| # | Finding | File |
|---|---|---|
| F1 | Decode error makes the Stop guard fail open | `.ai/hooks/stop_guard.py` |
| F2 | Same shape in the SessionStart hook | `.ai/hooks/session_start.py` |
| F3 | Blackboard emitted through locale-encoded stdout | `.ai/hooks/session_start.py` |
| F4 | Two AC-2 round-trip tests pass regardless of encoding | `test_utf8_io.py` |
| F5 | `test_ascii_content_is_byte_for_byte_unchanged` asserts a tautology | `test_utf8_io.py` |
| F6 | AST audit misreads a positional binary mode; re-parses per test | `test_utf8_io.py` |

## Out of scope

- Platform-dependent newline translation (`ctx-003`). Real, separate, untouched.
- `_support.py`'s unencoded text I/O (`ctx-004`). Needs its own task.
- The diff-scope base problem. It is an orchestrator design gap, not an
  encoding defect, and it needs a decision rather than a patch.
- Any behaviour change beyond the six findings.

## Acceptance criteria

1. A hook whose input file contains invalid UTF-8 **blocks** rather than
   exiting 1. Verified by feeding `stop_guard.py` malformed bytes and asserting
   exit 2 with a reason on stderr.
2. `session_start.py` emits a block containing non-cp1252 text without raising,
   on Windows. Verified by injecting Japanese text and asserting exit 0 and the
   text present in stdout.
3. No test in `test_utf8_io.py` passes when the encoding argument it exists to
   check is removed. Verified by mutating the source in a temp copy and
   asserting the corresponding test fails.
4. The AST audit classifies `open(path, 'rb')` and `os.fdopen(fd, 'rb')` as
   binary, not as offenders.
5. The full suite passes on 3.9 and 3.12.

## Constraints

- Standard library only.
- A guard must fail **closed**: exit 2 with a reason, never exit 1.
- Do not weaken or delete an existing test. F4 and F5 are strengthened in
  place, not removed.
- Keep the four TASK-006 files' encoding behaviour exactly as it is; this task
  fixes what surrounds it.
