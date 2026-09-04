# TASK-009 implementation notes

Reconciled both conflicted hooks and repaired the test harness that would have
made the new coverage meaningless. Three files modified, none created,
`_support.py` untouched — as the approved plan v2 declares.

## What changed

### `.ai/hooks/stop_guard.py`

The conflict hunk carried TASK-007's `Unreadable` / `read_guarded()` on HEAD and
TASK-008's `repo_root()` / `task_directory()` on `main`. Both are now present:
rooting helpers first, then the exception class and the guarded reader, which
matches the order the auto-merged `session_start.py` body already used.

Nothing else in the file needed touching — the auto-merge had already woven the
call sites together, so `context_blocks()` locates through `task_directory()`
and reads through `read_guarded()`, and `main()` resolves `task_dir` the same
way before reading `implementation.md` through `read_guarded()`. Every read the
verdict depends on goes through the helper; the `Unreadable` handler still
returns `BLOCK`, never exit 1.

### `.ai/hooks/session_start.py`

Resolved to `main`'s `constitution.is_file()` pre-check, reading through
TASK-007's `read_or_warn()` — the Q-1 answer. Both halves of the hunk were
wrong on their own: HEAD referenced `CONSTITUTION`, which `main` had renamed,
and `main` read the constitution with a bare `read_text()`, silently dropping
the degradation behaviour. The label stays `CONSTITUTION_RELATIVE`. Added a
two-line comment on why the pre-check stays: an absent constitution is not a
fault, one that exists and cannot be decoded is.

### `test_hook_resilience.py`

`HookCase.run_hook` now **assigns** `ORCHESTRATOR_TASK_DIR`,
`ORCHESTRATOR_REPO_ROOT` and `ORCHESTRATOR_CONSTITUTION` to the fixture's own
throwaway tree rather than inheriting them, with per-test overrides still
applied last. Without this every case in the module would have been answered
from the live task tree once the hooks prefer an exported path over cwd.

Generalised `write_notes` / `write_block` / `write_raw_block` with an optional
`where=`, and added `exported_tree()`, which builds a second task tree in its
own temp dir — deliberately unreachable from cwd, so a case asserting on its
contents is asserting that the exported path won. Existing helper calls are
unchanged and no existing test was weakened or removed.

Four new cases across three classes: `HookRootingResilienceInteractionTests`
(AC-1, AC-2), `ExportedConstitutionResilienceTests` (AC-3),
`HookHarnessIsolationTests` (AC-4).

## Each new test was checked for discrimination

Requirement 4 asks for tests that fail if either half is reverted, so I
reverted each half in turn, observed the failure, and restored it. This is the
evidence behind that claim, not an inference from the tests passing:

| revert | result |
|---|---|
| `task_directory()` → cwd-only | AC-1 fails: exit **0**, not a spurious block |
| `read_guarded(notes)` → bare `read_text` | AC-1 fails: exit **1**, the silent-failure code |
| `emit()` → `sys.stdout.write` | AC-2 fails: `UnicodeEncodeError`, exit 1 |
| `CONSTITUTION_RELATIVE` → `constitution` | AC-3 fails: heading leaked the absolute temp path |
| `read_or_warn(constitution)` → bare `read_text` | AC-3 and the source-level helper test both fail |
| `run_hook` rooting assignments removed | AC-4 fails: exit 0, the decoy answered the guard |

AC-1 fails on *both* reverts with distinguishable symptoms, which is what makes
it an interaction test rather than two half-tests.

Per ctx-001 (5), AC-2's forced `PYTHONIOENCODING=cp1252` subprocess codec was
re-checked under a UTF-8 ambient locale (`PYTHONUTF8=1`, `LANG=C.UTF-8`, the CI
condition): the `emit()` revert still fails there, so the criterion stands
alone and does not lean on the host locale.

## Validation commands run

Interpreter: `py -3` (Python 3.13.15 on Windows). Bare `python` on this host is
the Microsoft Store shim and does not execute.

| criterion | command | result |
|---|---|---|
| AC-1 | named unittest case | pass |
| AC-2 | named unittest case | pass |
| AC-3 | three named unittest cases | pass |
| AC-4 | named unittest case | pass |
| AC-5 | `-m unittest -v test_hook_resilience` | pass, 26 tests |
| AC-6 | byte-compile both hooks + conflict-prefix line scan | pass, rc=0, no marker lines |
| AC-7 | full discovery | **777 discovered, OK** (see caveat below) |
| AC-8 | `py_compile` of both workflow scripts | pass, rc=0 |
| AC-9 | `json.tool` task-state schema | pass, rc=0 |
| AC-10 | `json.tool` plan schema | pass, rc=0 |

`test_hook_resilience` also passes under the UTF-8 ambient locale CI uses.

## AC-7 caveat: 6 out-of-scope failures under exported rooting variables

Stated plainly because it will show up again. AC-7 passes (777/777) in an
environment without the `ORCHESTRATOR_*` rooting variables. Run from *inside*
an orchestrated worker session — where they are exported — six tests fail, none
of them in a file this task declares:

- `test_plan_authorization.DeclaredInfrastructureTests` — 3
- `test_context_bus.HookBehaviourTests` — 2
- `test_hardening.PreToolUseWriteTests.test_denies_editing_the_hooks_themselves` — 1

I isolated the cause rather than assuming it: starting from a cleared
environment, exporting `ORCHESTRATOR_TASK_DIR` *alone* flips the two
`test_context_bus` cases, and exporting `ORCHESTRATOR_REPO_ROOT` alone flips
them too. These fixtures inherit the ambient rooting variables, so the hooks
they invoke read the live TASK-009 tree instead of the fixture tree. That is
the same harness defect AC-4 fixes in `test_hook_resilience.py`, present in
three modules outside this task's declared `files_to_modify` — so I did not
touch them. The exported-path preference itself is `main`'s TASK-008 behaviour,
not something this reconciliation introduced.

Two things I want to be accurate about:

- I did **not** establish these six pass on the pre-merge baseline tree. That
  tree's hooks do not parse at all, so there is no meaningful comparison to
  draw; the mechanism above is the evidence, not a before/after run.
- Whether validation reproduces them depends on the environment
  `run_validation_check` hands the criterion. ctx-001 (2) records that it
  passes no `env=` and that the orchestrator environment does not set
  `ORCHESTRATOR_TASK_DIR`; on that basis AC-7 should pass. I am reporting the
  observation, not predicting the orchestrator's result.

## Git state

The merge is **not** staged or committed. Both hooks are still `UU` in the
index; `git add` and the single merge commit are the developer's, per the plan
constraint that no intermediate commit with unparsable hooks enters history and
that the worker creates no commits unless authorised. The working-tree bytes
are the reconciled result, which is what the baseline delta compares against.

## Deviations from the approved plan

None in scope, files, or approach. Two additions the plan did not spell out,
both inside the declared files and required to make the declared tests work:
the `where=` parameter on the three write helpers, and `exported_tree()`. The
redundant `import shutil` / `import tempfile` inside `HookCase.setUp` were
removed in favour of module-level imports the new fixtures also need.

## Not done

Nothing from the approved scope is outstanding. The `PYTHONIOENCODING`
harness finding recorded against TASK-007 remains untouched, as the plan
requires. CI on `ubuntu-latest` × py3.9/py3.12 has not been run — I verified
stdlib-only, `%`-formatted, 3.9-compatible constructs and a UTF-8 locale run
locally, but I cannot claim a CI result I did not observe.
