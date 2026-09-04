# TASK-007 implementation notes

**Authored by the developer-facing session, not by the `implementer` worker.**
The developer asked for these fixes directly, so this task did not go through
`plan` / `approve` / `implement`. `state.json` is therefore still at `NEW` and
was deliberately left that way: advancing it by hand would be exactly the kind
of unearned evidence the workflow exists to prevent. If this needs to run
through the pipeline for the audit trail, the requirement is written and ready.

## F1 — the Stop guard failed open on a decode error

`stop_guard.py` read `implementation.md` and `context.jsonl` with a strict
codec and no handler. Malformed bytes raised `UnicodeDecodeError`, the script
exited 1, and only exit 2 blocks — so the write-back guard did not run and the
session ended anyway.

Added an `Unreadable` exception and a `read_guarded()` helper. `main()` catches
`Unreadable` and returns `BLOCK` with the reason on stderr. The guard now
refuses when it cannot confirm the write-back, rather than assuming it happened.

Worth stating plainly: TASK-006 made this **worse**, not better. The previous
locale decode on Windows rejected five byte values; UTF-8 rejects any malformed
high-byte sequence, so the change widened the set of inputs that reached the
fail-open path.

## F2/F3 — the SessionStart hook lost injection two ways

Same read shape, but an injector must not block, so it degrades instead:
`read_or_warn()` returns what it can and names the gap on stderr. A hook that
silently injected less context would hide a lost blackboard.

The output side was the sharper bug. The hook read UTF-8 and then wrote through
`sys.stdout`, which uses the locale codec — so a block containing anything
outside cp1252 raised `UnicodeEncodeError`, the hook died, and injection was
lost. That is the same failure the TASK-006 requirement describes, one layer
further out than the acceptance criteria looked. `emit()` now encodes
explicitly and writes to `sys.stdout.buffer`, with a text-layer fallback for a
replaced stdout that has no buffer.

## F4/F5 — two AC-2 tests could not fail

`test_context_block_round_trips_unicode` and `test_context_file_bytes_are_utf8`
went through `json.dumps` with `ensure_ascii=True`, so the bytes on disk were
pure ASCII whatever `encoding=` was passed. Both passed with the fix removed.

Not deleted — the behaviour they exercise is real, it just is not the encoding
claim. Renamed to `test_context_block_survives_json_escaping` and
`test_context_file_is_pure_ascii_by_json_escaping`, with docstrings saying so
and pointing at `test_requirement_text_round_trips_through_task_creation`, which
goes through a raw `write_text` and does depend on the encoding. The second now
asserts the real invariant (no high bytes on disk) instead of decoding ASCII as
UTF-8 and finding that it works.

`test_ascii_content_is_byte_for_byte_unchanged` asserted
`assertEqual(raw, raw.decode("ascii").encode("ascii"))` — an identity on any
byte string that decodes at all. It now encodes the recovered text under both
UTF-8 and cp1252 and requires all three to agree, which is the actual claim
that makes the encoding change safe for every artifact already committed.

## F6 — the audit misread binary modes and re-parsed per test

`is_binary_mode` inspected only `args[0]`. `Path.open("rb")` puts the mode
first, but `open(path, "rb")` and `os.fdopen(fd, "rb")` put it second, so both
were classified as text I/O and reported as offenders — a hard failure for
legitimate binary I/O, and the inverse of the risk the plan named. It now scans
every positional argument; a path or descriptor is never spelled `"rb"`.

`text_io_calls` now caches per path. Five tests audited the same four files, one
of them ~4000 lines, so the same sources were read and parsed twenty times.

## New: proof the audit can fail

TASK-006's implementer verified by hand, in `ctx-012`, that the audit
discriminates — running its logic against `git show HEAD:...` and getting 7
offenders versus 0 on the current tree. A hand check is not a test.
`calls_in_source()` was split out of `text_io_calls()` so source text can be
audited without writing a broken copy to disk, and `AuditDiscriminatesTests`
now strips one `encoding="utf-8"` from the real orchestrator source in memory
and asserts an offender appears.

## Not done, on purpose

- Newline translation (`ctx-003`): real, separate, out of scope.
- `_support.py`'s unencoded text I/O (`ctx-004`): needs its own task; the
  harness still writes fixtures in the locale encoding and the suite passes
  only because every fixture is ASCII.
- `TEXT_IO_NAMES` matching any `.open` attribute call, so an unrelated
  `foo.open()` in a scoped file would be asked for an encoding. Restricting it
  to Path-like receivers is guesswork without a real case; the reported hard
  failure was the binary-mode bug, and that is fixed.
- The diff-scope base problem. It needs a decision, not a patch — see below.

## Note for whoever runs this through validation

Diff-scope has flipped from vacuous to over-inclusive. `main` is still at
`39e53dd` while this branch sits on `679d5c9`, so `merge-base...HEAD` includes
all 72 files from the previous commit. Every one would read as an undeclared
scope violation. `main` needs to advance before TASK-007 is validated.

---

# Fix run — 2026-09-04, `implementer` under approved plan v5

## What the recorded failure actually was

The task was handed to me as `fix-precondition: plan_rejected`, with a
classifier rationale stating that `state.json` records `status FAILED`,
`plan_version 4`, and `plan_file` naming `plan-v3.json`.

**That rationale does not match the tree.** As read this run:

- `state.json` → `"status": "IMPLEMENTING"`, `"plan_version": 5`,
  `"plan_file": ".ai\tasks\TASK-007\plan-v5.json"`.
- `events.jsonl` → `PLAN_APPROVED` for `plan-v5.json` with
  `plan_sha256 8b8621b147f2981d6e75ce58228506520cc64cf4e8c5a7d57269a6889460cadf`.
- Re-hashing `plan-v5.json` this run yields that same digest, and it matches
  the approval recorded on the blackboard in `ctx-004`.

So an approval *does* cover the bytes of the current `plan_file`. The
`failure_reason` string is stale evidence left on `state.json` from the
pre-replan attempt, and the classifier reasoned from it. The two plan-v5
infeasibility claims in the rationale are also about plan v3, not v5: plan v5
has `files_to_create: []` and declares `test_hook_resilience.py` under
`files_to_modify`, which is exactly the correction ctx-003 demanded.

I did not change `state.json` to reflect this — that is orchestrator-owned
evidence. Flagging it here is the correct channel.

## What I changed

**No source edits were required.** Every item plan v5 specifies was already
present in the working tree, and — this is the part that matters — it is
present *as this task's delta*, not as pre-existing tree state.

Verified against the immutable baseline (`baseline.json`,
`8dcd85c3421827c3...`, 60 entries, `truncated: false`) by hashing every entry:

```
changed vs baseline: ['test_hook_resilience.py', 'test_utf8_io.py']
```

Exactly the two paths in `files_to_modify`, and nothing else. In particular
`.ai/hooks/stop_guard.py` and `.ai/hooks/session_start.py` are byte-identical to
the baseline, which is what the plan's second constraint requires.

The only file I created this run was a throwaway baseline-comparison script,
`.ai/tmp_baseline_check.py`. It was undeclared, so I deleted it; the delta above
was recomputed afterwards and is clean.

## Verification actually run

Every acceptance criterion's `verify` command, with `{python}` = `py`:

| AC | Result |
|----|--------|
| AC-1 | pass — 12 tests |
| AC-2 | pass — 7 tests |
| AC-3 | pass — 7 tests |
| AC-4 | pass — 8 tests |
| AC-5 | pass — grammar check, then **700 tests discovered, all passed**, exit 0 |
| AC-6 | pass — CI matrix still pins 3.9 and 3.12 |

Also the remaining `.ai/validation.json` checks: `compile` ok, both schema
checks ok.

AC-3 is the one worth naming: the four mutation tests each ran the covered
round-trip method against a clean in-memory orchestrator and against one with a
single encoding argument cut, and asserted the pass→fail transition. They pass,
so those tests are encoding-sensitive rather than decorative — which is the
defect F4/F5 were about.

## Correction to the earlier note above

The previous note says this branch sits on `679d5c9` and warns that a
`merge-base...HEAD` diff would show 72 files. `HEAD` is now `a930028`
(`679d5c9` → `811626d` → `fd35909` → `a930028`), and the baseline was captured
at `811626d`. Validation compares against `baseline.json`, not against a
merge-base diff, so the over-inclusive-scope warning no longer applies on the
baseline path. The earlier TASK-007 work was committed in `a930028`, before the
baseline was captured — which is why the hooks read as unchanged and why plan
v5 correctly rescopes this task to the two test files.

## Not done

- No production behaviour was changed, by design: plan v5 treats the hook
  hardening as already-landed and forbids touching those files.
- The stale `failure_reason` on `state.json` is still there. Clearing it is the
  orchestrator's call, not mine.
