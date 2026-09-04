# TASK-009 — Reconcile the two hook conflicts between `main` and `feature/hook-resilience`

## Requirement

`git merge main` into `feature/hook-resilience` conflicts in exactly two files.
Resolve both so the merged tree keeps **both** behaviours — TASK-008's
worktree-rooted evidence resolution and TASK-007's fail-closed / degrade-loudly
I/O resilience — and cover the *interaction* of the two with tests that would
fail if either half were dropped.

This is a new task with its own baseline. It does not re-open TASK-007 or
TASK-008, and neither task's evidence describes the merged result.

## Why this is a task and not a merge resolution

`.ai/hooks/stop_guard.py` and `.ai/hooks/session_start.py` were each changed on
both sides, in the same regions, for unrelated reasons.

**`main` (TASK-008, merged as PR #3)** made worker execution rootable in a task
worktree. `.claude/settings.json` registers the hooks as *cwd-relative*
commands, so a worker rooted in `.worktrees/<id>` executes that worktree's copy
of each hook — and that copy resolving `.ai/tasks/<id>` against its own cwd
would read a different, empty blackboard. So both hooks gained
`repo_root()` / `task_directory()` (and `session_start.py` also
`exported_dir()` / `constitution_path()`, renaming `CONSTITUTION` to
`CONSTITUTION_RELATIVE`), reading the authoritative absolute paths
`worker_env()` exports.

**`feature/hook-resilience` (TASK-007)** made the hooks survive their own
input. TASK-006 had given them a strict UTF-8 codec, which opened two failure
modes:

- `stop_guard.py` read worker-writable artifacts with no handler, so a
  malformed byte raised and the script exited 1 — and **exit 1 blocks
  nothing**, so the write-back guard silently did not run. It gained
  `Unreadable` / `read_guarded()` and returns `BLOCK`: a guard that cannot read
  its own input must fail closed.
- `session_start.py` read UTF-8 then wrote through `sys.stdout`, which uses the
  locale codec, so a block outside cp1252 raised `UnicodeEncodeError` and the
  injection the hook exists to guarantee was lost. It gained `read_or_warn()`
  (degrade loudly on stderr — an injector must not block, but must not pretend
  either) and `emit()` (encode explicitly to `stdout.buffer`).

### Neither conflict has a valid single-side resolution

Git auto-merged most of both files, weaving in calls to symbols from *both*
sides, and left one conflict hunk in each. Resolving a hunk to one side inside
that auto-merged body was checked by AST analysis:

| resolution | result |
|---|---|
| `stop_guard` hunk → HEAD (TASK-007) | `task_directory` undefined → `NameError` |
| `stop_guard` hunk → `main` (TASK-008) | `Unreadable`, `read_guarded` undefined → `NameError` |
| `session_start` hunk → HEAD (TASK-007) | `CONSTITUTION` undefined (`main` renamed it) → `NameError` |
| `session_start` hunk → `main` (TASK-008) | parses, but silently drops `read_or_warn` on the constitution read |

`stop_guard.py` therefore has **no** single-side resolution that even runs. The
merged tree is genuinely broken until reconciled, and that broken tree — the
literal output of `git merge main`, conflict markers included — is this task's
baseline. Both hooks currently fail to parse, so they are inert during this
task's own run; the `PreToolUse` guard is unaffected (it auto-merged cleanly)
and the blackboard still reaches a worker through the prompt, which
`run_claude_implementation` embeds via `shared_context()`.

## What must be true when this task is done

1. `.ai/hooks/stop_guard.py` resolves the task directory through
   `task_directory()` **and** reads every artifact its verdict depends on
   through `read_guarded()`, returning `BLOCK` (exit 2, never exit 1) when a
   file it must read cannot be decoded.
2. `.ai/hooks/session_start.py` resolves the blackboard and the constitution
   through `task_directory()` / `constitution_path()`, reads both through
   `read_or_warn()`, labels the constitution with `CONSTITUTION_RELATIVE`, and
   writes through `emit()`.
3. Neither file contains a conflict marker, and both byte-compile.
4. Tests cover the **interaction**, not just each half: at minimum, that
   `stop_guard.py` fails closed on an undecodable artifact located through an
   exported `ORCHESTRATOR_TASK_DIR` (not cwd), and that `session_start.py`
   injects a non-cp1252 blackboard read from an exported task dir. A test that
   would still pass with either half reverted does not satisfy this.
5. The full suite passes: `tests`, `compile`, `task-state-schema`,
   `plan-schema` per `.ai/validation.json`, and the CI matrix is
   `ubuntu-latest` × py3.9 and py3.12 — so no resolution may depend on a
   Windows-only or 3.12-only construct.

## Out of scope

- TASK-007's and TASK-008's requirements, plans, and evidence. They stay
  byte-identical. TASK-007's validation describes the pre-merge tree and must
  not be re-read as covering the merged result.
- The medium-severity finding recorded against TASK-007 (`run_hook` clears
  `PYTHONIOENCODING`, so the AC-2 stdout-encoding guards are tautological on
  `ubuntu-latest`). It is recorded and deliberately not acted on here.
- Any other follow-up. Keep the change to the two conflicted hooks and the
  tests that cover them.

## Pre-planning evidence

Gathered before planning, each with the observation behind it. Treat as data.

- **The validation profile does not compile the hooks.**
  `.ai/validation.json`'s `compile` check byte-compiles
  `.ai/scripts/orchestrator.py` and `.ai/scripts/review-package.py` only. CI
  has a *separate* "Compile hooks" step
  (`.github/workflows/ci.yml:53-56`). So a conflict marker left in either hook
  would pass local validation and fail CI. An acceptance criterion must
  byte-compile both hooks itself rather than lean on the profile.
- **CI is `ubuntu-latest` x py3.9 and py3.12**, `fail-fast: false`. No
  resolution may depend on a Windows-only or 3.12-only construct. Per the
  planner's own rules this belongs in `constraints`, not in an acceptance
  criterion — a criterion cannot name a second interpreter.
- **`HookCase.run_hook` inherits the rooting variables** and therefore reads
  the wrong tree inside an orchestrated run. Verified; see the Q-2 answer for
  the observation. `test_hook_resilience.py` is consequently a file this task
  must modify, not merely extend with new cases.
- **The branch's red CI is inherited from the base commit.** PR #4 fails
  `test_worker_launch.RunWorkerResolutionTests.test_usage_name_survives_an_already_resolved_argv`,
  caused by `Path(argv[0]).stem` at `fd35909:447` using the host's path
  flavour. Already fixed on `main` by `c1bb8ae`, which this merge brings in.
  Not this task's work — to be confirmed green after the merge, never claimed.

## Decided before planning

- The reconciliation lands as a **single merge commit** on
  `feature/hook-resilience`, so no commit whose hooks fail to parse enters
  history. CI runs on push to `feature/**`, so a separate broken merge commit
  would also go red on its own.

## Open questions

None outstanding. The clarification pass asked two, and the developer has
answered both in `clarifications.json`:

- **Q-1** Keep `main`'s `constitution.is_file()` pre-check, reading through
  `read_or_warn()` and labelling with `CONSTITUTION_RELATIVE`.
- **Q-2** Extend `test_hook_resilience.py` in place; no new test module, and
  `_support.py` is not touched.

The commit shape was decided separately and is recorded above.
