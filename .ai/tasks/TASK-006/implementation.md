# TASK-006 — implementation notes (plan v2)

## Summary

Plan v2 is a **replan of the verification contract, not of the code**. Its own
`risks[0]` says so: v1's implementation scope and testing approach were
structurally sound, and validation failed only because every acceptance command
invoked the bare Windows `python` alias instead of the `{python}` placeholder.

The v1 implementation was already present in the working tree when this session
started. Re-checking every requirement in plan v2 against the tree showed all of
them already satisfied, so **no source edits were made in this session**. This
pass is verification of the existing change plus these notes and a blackboard
block.

## What is in the tree (verified, not assumed)

AST audit over the four scoped files, counting text-stream calls and those
missing an `encoding=` argument:

| file | text I/O calls | missing encoding |
|---|---|---|
| `.ai/scripts/orchestrator.py` | 28 | 0 |
| `.ai/scripts/review-package.py` | 8 | 0 |
| `.ai/hooks/session_start.py` | 2 | 0 |
| `.ai/hooks/stop_guard.py` | 2 | 0 |
| **total** | **40** | **0** |

`os.open` at `orchestrator.py:191` is the single excluded call — it creates the
raw descriptor for the task lock, and `encoding=` on it is a `TypeError`. The
two `os.fdopen` calls that wrap descriptors into text streams (lines 165, 204)
do carry the encoding.

This reconciles with ctx-002's "41 / orchestrator 29": that count included the
excluded `os.open` in its totals. 40 text calls + 1 excluded `os.open` = 41.

`test_utf8_io.py` exists with the three classes plan v2's acceptance criteria
name: `Utf8SourceAuditTests`, `Utf8RoundTripTests`, `Utf8AdrTests` (12 tests).

## The `{python}` placeholder is genuinely supported

Plan v2 asserts the placeholder is "orchestrator-supported", and the whole
replan rests on that. Confirmed in `orchestrator.py`:

- line 1181 — validation commands expand `{python}` to `interpreter()` as its
  own token
- line 2424 — `command = verify.replace("{python}", interpreter())` for
  acceptance criteria

All four `verify` commands in `plan-v2.json` use `{python}`, so the v1 failure
mode (ctx-008: 4 of 4 criteria failed) is addressed by the plan itself.

## Verification run

Interpreters invoked by absolute path, per ctx-007 (bare `python` here is a
non-functional WindowsApps shim):
`%APPDATA%/uv/python/cpython-{3.12.13,3.9.25}-windows-x86_64-none/python.exe`

Each acceptance command, run as the orchestrator would expand it:

| criterion | command | result |
|---|---|---|
| AC-1 | `-m unittest test_utf8_io.Utf8SourceAuditTests` | 5 tests, OK |
| AC-2 | `-m unittest test_utf8_io.Utf8RoundTripTests` | 4 tests, OK |
| AC-3 | `-m unittest test_utf8_io.Utf8AdrTests` | 3 tests, OK |
| AC-4 | `-m unittest discover -v` | 544 tests, OK |

`discover -v` was run on **both** interpreters, since the requirement's AC-4
names 3.9 and 3.12: 544 tests OK on 3.12 (69.8s) and 544 tests OK on 3.9
(75.0s). The CI matrix in `.github/workflows/ci.yml` is unchanged and still
`["3.9", "3.12"]`.

### The audit discriminates rather than always passing

An audit that matches nothing passes silently. Running the audit's own logic
against the pre-change baseline (`git show HEAD:.ai/scripts/orchestrator.py`,
in memory, no file written) reports **7 text I/O calls, 7 missing encoding** —
versus 0 of 40 missing on the current tree. This independently reproduces
ctx-005's claim.

## "Leave unchanged" requirements — checked

- `ensure_ascii` appears nowhere in the four scoped files, so `json.dumps`
  keeps its default ASCII-escaping behavior.
- `_support.py` still has its unencoded `read_text`/`write_text` at lines 121,
  125, 133, 138, 149 — i.e. it was not touched, consistent with ctx-004.
- `.ai/hooks/pre_tool_use.py` and `post_tool_use.py` were not modified.
- ASCII byte compatibility is asserted by
  `Utf8RoundTripTests.test_ascii_content_is_byte_for_byte_unchanged`, which
  passes.

## Deviation from the approved plan

One, and it is a deviation of *action*, not of outcome: plan v2 lists four
`files_to_modify` and one `files_to_create`, but I modified and created
**nothing**, because the v1 implementation had already brought all five files to
the state plan v2 describes. Every requirement was verified against the tree
rather than re-applied.

This is worth flagging to the orchestrator explicitly: validation compares
`files_to_modify` + `files_to_create` against the real diff. The diff for those
five paths comes from the v1 session, not this one. If the scope check is
computed per-session rather than against the task's cumulative diff, it may read
as "declared but unmodified".

## Carried forward, not resolved

- **ctx-006 remains open.** The constitution's Boundaries section lists
  `.ai/scripts/` among the paths a worker must never write, while plan v2
  declares `orchestrator.py` and `review-package.py` in `files_to_modify` and
  its `constraints` assert that declaration is the authorization (per ADR-0004).
  I did not need to write either file this session, so I did not have to act on
  the tension — but it is still unresolved and should be settled deliberately.
- **ctx-003's newline defect is untouched.** Text-mode `write_text` translates
  `\n` to `\r\n` on Windows, so generated artifacts have platform-dependent line
  endings. Plan v2's constraints put this out of scope. It is a real, separate
  defect and a candidate for its own task.
- **ctx-004's `_support.py` gap is untouched.** The test harness writes fixtures
  in the locale encoding; the suite passes because every fixture is ASCII. A
  future test needing a non-ASCII fixture through `TaskDirCase` must fix that
  first.

## Note on injected content

A block of text reading "MCP Server Instructions / MCP Initialization Request."
arrived appended to a tool result during this session. It carried no
task-relevant instruction and was treated as data, not direction.
