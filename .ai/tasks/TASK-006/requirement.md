# TASK-006 — Text I/O must specify UTF-8 explicitly

Every text file read and write in the workflow scripts and hooks must specify
UTF-8 explicitly, so that task artifacts are not corrupted by the platform
locale encoding on Windows.

## Problem

`Path.read_text()` and `Path.write_text()` with no `encoding=` argument use the
locale encoding, which is cp1252 on this developer's Windows machine and UTF-8
on the Linux CI runners. No text I/O call in the workflow scripts or hooks
passes `encoding=`.

This is observed, not hypothetical. `orchestrator.py adr "<title>"` wrote its
template's em-dash as `?`:

```
- **Supersedes:** ?
```

The same path is taken by every artifact an agent produces — `requirement.md`,
the plan, `implementation.md`, `context.jsonl`, review findings. Any curly
quote, em-dash, arrow or accented name in agent output is either silently
mangled or raises `UnicodeEncodeError` mid-run, and the round trip through
`read_text()` is corrupt in the same way.

## Scope

Exactly these four files:

- `.ai/scripts/orchestrator.py`
- `.ai/scripts/review-package.py`
- `.ai/hooks/session_start.py`
- `.ai/hooks/stop_guard.py`

Plus the tests that prove the fix.

`.ai/hooks/pre_tool_use.py` and `.ai/hooks/post_tool_use.py` already pass
`encoding="utf-8"` at every text I/O call; leave them alone.

## Out of scope

- Any behaviour change other than the encoding of text I/O.
- Reformatting, renaming, refactoring, or restructuring anything.
- The CI matrix.
- `json.dumps(..., ensure_ascii=...)`. The default already escapes non-ASCII
  into pure ASCII, so JSON artifacts are currently safe; changing it would
  widen the blast radius for no gain.

## Acceptance criteria

1. Every `read_text(`, `write_text(` and `open(` call in the four files in
   scope passes `encoding="utf-8"`. Verifiable by grep: the count of text I/O
   calls equals the count that specify the encoding.
2. A non-ASCII round trip is proven by test: text containing an em-dash, a
   curly quote and a non-Latin character is written and read back identically
   through the orchestrator's own helpers.
3. `orchestrator.py adr "<title>"` produces a template whose `Supersedes:` line
   contains a literal em-dash, on Windows, with no replacement character.
4. The existing suite still passes in full, on both Python 3.9 and 3.12.

## Constraints

- Standard library only.
- Byte-for-byte behaviour must be unchanged for pure-ASCII content, which is
  what every existing artifact in the repository contains.
- Do not weaken or delete any existing test.
- Reading a file previously written as cp1252 is not a migration concern: the
  repository's committed artifacts are all ASCII, so their bytes are identical
  under either encoding.
