# Review Summary

## Task

TASK-006

## State

VALIDATED

## Branch

None

## Plan Version

2

## Approved Plan

`.ai\tasks\TASK-006\plan-v2.json`

## Diff Base

`origin/main` at `39e53dd3f28a1187eb7528c0789f86dbc115a8dd`

Excludes `.ai/tasks/`.

## Files Changed

No files changed against the diff base.

## Git Diff

Empty against the diff base.

## Validation

- Command: `C:\Users\manka\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe -m unittest -v`
- Return code: 0
- Tests run: 544
- Result: PASSED

## Acceptance Criteria

| Criterion | Result | Verify |
|---|---|---|
| AC-1 Every text-mode read_text, write_text, open, Path.open, and os.fdopen call in the four scoped files explicitly specifies encoding="utf-8", while raw descriptor-only os.open calls remain excluded. | PASS | `{python} -m unittest test_utf8_io.Utf8SourceAuditTests` |
| AC-2 The orchestrator's artifact helpers preserve an em dash, curly quote, and non-Latin character through write/read round trips, and pure-ASCII output remains byte-compatible. | PASS | `{python} -m unittest test_utf8_io.Utf8RoundTripTests` |
| AC-3 ADR creation produces a Supersedes line containing the literal UTF-8 em dash without a replacement character or question-mark substitution. | PASS | `{python} -m unittest test_utf8_io.Utf8AdrTests` |
| AC-4 The complete repository unittest suite passes on the validation interpreter with a non-empty, parseable test count. | PASS | `{python} -m unittest discover -v` |

## Diff Scope

- Declared in the plan: .ai/hooks/session_start.py, .ai/hooks/stop_guard.py, .ai/scripts/orchestrator.py, .ai/scripts/review-package.py, test_utf8_io.py
- No undeclared files changed.

## Reviewer Findings

- **correctness**: 4 finding(s). Examined: requirement.md, plan-v2.json and implementation.md for TASK-006 as the stated contract; Every text I/O call site in .ai/scripts/orchestrator.py found by grep for read_text(/write_text(/open(/fdopen( (28 calls: lines 146,165,196,204,649,757,808,825,878,895,921,1084,1145,1457,1500,1615,2147,2209,2246,2628,2799,2902,2951,3017,3553,3613,4003,4044) — all carry encoding="utf-8"; multi-line call sites at 1615, 2799, 3553, 3613 read directly to confirm the keyword is present; All 8 text I/O calls in .ai/scripts/review-package.py (74, 98, 109, 145, 209, 247, 286, 381), including the multi-line write_text at 381; .ai/hooks/session_start.py in full (2 text reads, lines 32 and 53) and .ai/hooks/stop_guard.py in full (2 text reads, lines 36 and 63); os.open at orchestrator.py:191 (raw descriptor, correctly left without encoding) and the two os.fdopen text wrappers at 165 and 204; subprocess text-mode call sites in the two scripts (orchestrator 430-448, 506, 1187, 1306, 2427, 2464, 2481, 3315, 3331, 3368, 3457, 3899, 3934, 3981; review-package 32) for locale-dependent decoding; test_utf8_io.py in full: the AST audit helpers (call_label, is_os_open, is_binary_mode, text_io_calls, utf8_keyword) and all 12 tests in Utf8SourceAuditTests / Utf8RoundTripTests / Utf8AdrTests, traced against AC-1..AC-3; _support.py TaskDirCase/quiet/load_orchestrator to confirm the behavioural tests run chdir'd into a throwaway tree, and orchestrator.py:74-84 (relative CONSTITUTION_PATH/ADR_DIR) plus run_adr (3533-3574) to confirm ADR tests cannot touch the real .ai/adr; Plan v2's claim that {python} is orchestrator-supported, verified at orchestrator.py:1180-1184 and 2424.
  - `medium` Two of the four AC-2 round-trip tests pass regardless of the encoding argument — test_utf8_io.py:222 (AC-2)
    test_context_block_round_trips_unicode (line 206) and test_context_file_bytes_are_utf8 (line 222) exercise append_context_block/read_context, which serialise through json.dumps with the default ensure_ascii=True. The bytes on disk are therefore pure ASCII (\uXXXX escapes) whatever encoding= is passed, so the write cannot mangle or raise, the JSON round trip restores the original str, and raw.decode('utf-8') plus assertNotIn(REPLACEMENT) hold trivially. Both tests would still pass with encoding= removed from orchestrator.py:878 and 825 on any host, i.e. they cannot fail for the defect they are named for. AC-2 is not left unproven — test_requirement_text_round_trips_through_task_creation (line 241) goes through create_task's raw write_text and does depend on the encoding — but the two context-block tests contribute no signal and will read as coverage that does not exist.
  - `low` test_ascii_content_is_byte_for_byte_unchanged asserts a tautology, not byte-for-byte equality — test_utf8_io.py:265 (AC-2)
    The assertion is assertEqual(raw, raw.decode('ascii').encode('ascii')). Decoding as ASCII and re-encoding as ASCII is an identity on any byte string that decodes at all, so the test really only asserts "the file is pure ASCII" (it fails via UnicodeDecodeError, not via assertEqual). It never compares against the bytes the pre-change code produced, so it does not establish the requirement's constraint that ASCII artifacts are byte-for-byte unchanged, which is what its name and docstring claim.
  - `low` AST audit misreads a positional binary mode and any non-file .open(), producing spurious offenders — test_utf8_io.py:73 (AC-1)
    is_binary_mode only inspects node.args[0] for a positional mode. For open(path, 'rb') and os.fdopen(fd, 'rb') the mode is args[1], so those calls are classified as text I/O and reported as offenders for lacking encoding= — the opposite of the plan's stated risk, and a hard failure for legitimate binary I/O. Relatedly, TEXT_IO_NAMES matches any attribute call named open other than os.open, so an unrelated foo.open() in a scoped file would also be demanded to take an encoding. Neither case exists in the four scoped files today (they use read_bytes/write_bytes), so AC-1 currently passes; the risk is a false failure for a future in-scope change, not a missed violation.
  - `low` session_start.py still emits blackboard content through the locale-encoded stdout — .ai/hooks/session_start.py:85 (AC-1)
    The hook reads context.jsonl as UTF-8 (line 32) and then writes the assembled blocks with sys.stdout.write at line 85. sys.stdout on Windows uses the locale codec, so a block whose content contains a character outside cp1252 (e.g. the Japanese text the new tests write) raises UnicodeEncodeError, the hook exits with a traceback, and the blackboard injection the hook exists to guarantee is lost. This is the same 'raises UnicodeEncodeError mid-run' failure mode the requirement's Problem section describes, in a file that is in scope, though the acceptance criteria confine themselves to read_text/write_text/open, so it is a residual gap rather than an AC-1 violation.
- **performance**: 1 finding(s). Examined: `.ai/tasks/TASK-006/requirement.md`, `plan-v2.json` and `implementation.md` for the declared scope of the change (encoding-only edits to four workflow files plus a new `test_utf8_io.py`); `test_utf8_io.py` in full: AST-audit helpers (`text_io_calls`, `utf8_keyword`), the five `Utf8SourceAuditTests`, the four `Utf8RoundTripTests` and the three `Utf8AdrTests` — looking at per-test file reads, repeated parses, and whether any test spawns a process needing a timeout; Every `read_text`/`write_text`/`open`/`fdopen` site in the four scoped files (`.ai/scripts/orchestrator.py`, `.ai/scripts/review-package.py`, `.ai/hooks/session_start.py`, `.ai/hooks/stop_guard.py`) via grep, to see whether an `encoding=` argument was added at a site that sits inside a loop or a hot path; `orchestrator.py` lines 140-210 (state load/save, task lock), 800-930 (event append, `read_context`, `append_context_block`, `render_constitution`, `render_adr_index`) and `run_adr` at 3533-3563, i.e. the call sites the round-trip and ADR tests exercise.
  - `low` AST audit re-reads and re-parses the same four source files once per test — test_utf8_io.py:92 (AC-1)
    `text_io_calls()` calls `path.read_text()` + `ast.parse()` on every invocation (test_utf8_io.py:92), and `test_every_encoding_is_utf8` re-implements the same walk with its own `ast.parse` (line 159). Across `test_audit_actually_finds_calls`, `test_every_text_io_call_specifies_an_encoding`, `test_every_encoding_is_utf8` and `test_low_level_os_open_is_not_given_an_encoding` that is roughly 13 full reads and parses of the scoped sources, of which `orchestrator.py` is ~4100 lines, when one parse per file cached at module level (or in `setUpClass`) would serve all four tests. The cost is bounded and small relative to the 544-test suite, so this is a tidiness/scaling point rather than a defect: it grows linearly with each audit test added.
- **security**: 1 finding(s). Examined: TASK-006 requirement.md, plan-v2.json and implementation.md for declared scope and constraints; Working-tree state of the four scoped files (.ai/scripts/orchestrator.py, .ai/scripts/review-package.py, .ai/hooks/session_start.py, .ai/hooks/stop_guard.py); note that .git/logs/HEAD shows HEAD == base 39e53dd, so the change under review exists only as uncommitted working-tree content and no committed diff was available to me; New file test_utf8_io.py in full: AST-only inspection of sources (no execution of repo content), fixture isolation via _support.TaskDirCase (tempfile.mkdtemp + os.chdir with cleanup), so writes land under a throwaway cwd rather than the real .ai/tasks or .ai/adr; Every encoding=/errors= site in .ai/scripts (grep): all 40+ text I/O and subprocess calls pass a literal encoding="utf-8"; no errors="ignore"/"replace"/surrogateescape was introduced, so no lossy-decode masking; Command construction in the touched files: subprocess.run call sites in orchestrator.py (lines 430-448, 506-514, 1187-1192) and review-package.py git() (line 32) use argv lists with no shell; the shell=True acceptance-criteria runner at orchestrator.py:2427-2435 and its {python} substitution at 2424 are pre-existing code, unchanged by an encoding-only change; Approval hash path: orchestrator.sha256_of (line 587) and pre_tool_use.py:155 both hash read_bytes(), so the read-encoding change cannot alter or weaken the hash-bound approval check; Permission surface: .claude/settings.json hook matchers (Edit|Write|NotebookEdit|MultiEdit|Bash for PreToolUse) and the set of files under .ai/hooks — no new hook script, no relaxed matcher, and pre_tool_use.py/post_tool_use.py carry no change; Prompt-injection surface of session_start.py, which renders context.jsonl block content verbatim into worker context.
  - `low` Strict UTF-8 decode in the Stop guard is unguarded, and a decode error makes the guard fail open — .ai/hooks/stop_guard.py:63 (AC-1)
    stop_guard.py now reads worker-writable artifacts with a strict codec: implementation.md at line 63 and context.jsonl at line 36. Neither read is wrapped in try/except, so bytes that are not valid UTF-8 raise UnicodeDecodeError, the script dies with exit status 1, and the module's own docstring records that only exit 2 blocks — exit 1 blocks nothing. The result is that undecodable bytes in a file the worker itself controls end the session without the blackboard write-back guard ever running. Under the previous locale decode on Windows (cp1252) only five byte values were undecodable; UTF-8 rejects any malformed high-byte sequence, so the set of inputs that trips this path is wider after the change than before it. session_start.py has the same shape at lines 32 and 53: a decode error there aborts blackboard injection instead of degrading loudly. This is a guard weakening rather than an exploit; it needs a worker to place non-UTF-8 bytes in its own artifact, which the orchestrator's own ASCII-escaping json.dump will not do. A try/except OSError/UnicodeDecodeError that returns BLOCK with the reason would make it fail closed.

## Evidence

- Implementation record: implementation.md
- Validation record: validation.md
- Machine-readable tests: test-results.json
- Event log: events.jsonl
- Reviewer findings: review-findings.json
