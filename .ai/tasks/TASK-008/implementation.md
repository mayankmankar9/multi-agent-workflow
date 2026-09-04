# TASK-008 — implementation notes

Plan v4 (sha256 `3bb2bdca1754`). This is the continuation of the run recorded in
ctx-005, which an external worker session-quota limit truncated after it had
written the production changes and `test_run_driver.py`. I completed the work
rather than reverting it, as that block advised.

## What the interrupted run had already done, and what I did to it

Present on disk when I started: `.ai/scripts/orchestrator.py` (+936),
`.ai/hooks/pre_tool_use.py`, `.ai/hooks/session_start.py`,
`.ai/hooks/stop_guard.py`, `CLAUDE.md`, `test_run_driver.py` (+787). I read all
of it, made no change to the production code or to `CLAUDE.md`, and changed
`test_run_driver.py` only to repair the two broken fixtures below.

### The two recorded failures were fixture defects in the new tests

Both were in `test_run_driver.py`, and neither indicated a defect in the verb.

1. `RecoverInterruptedImplementationTests.test_recovery_event_records_reason_and_developer`
   never called `self._interrupted()`, so no `state.json` existed and
   `run_recover` → `load_state` raised
   `FileNotFoundError: Task state not found: .ai/tasks/TASK-999/state.json`.
   Fix: added the missing fixture call. No assertion changed.
2. `RecoverValidationRouteTests.test_recovered_task_requires_validation_before_validated`
   wrote its `.ai/validation.json` *after* `write_baseline()`, so the validation
   profile counted as task-produced, was undeclared, and validation failed on
   `diff-scope: undeclared files changed: .ai/validation.json` — reaching
   `FAILED` instead of `VALIDATED`. Fix: write the profile before the baseline
   is captured, which is what the fixture meant. No assertion changed.

Neither fix weakens or deletes anything: both tests still assert exactly what
they asserted, and both now exercise the path they were written for.

## What I added

The remaining eight declared test modules were untouched. Each now carries the
acceptance criteria the plan assigns it, written inline (no change to
`_support.py`, which the plan deliberately does not declare).

- `test_task_baseline.py` — `ForeignTaskDeltaTests` (AC-7),
  `CommittedEvidenceTests` (AC-8), `RecordedWorktreeDeltaTests` (AC-9),
  `LegacyTaskRootTests` (AC-11). The worktree fixture is a real
  `git worktree add` on a different branch, so worktree-rooted git corroboration
  is distinguishable from the checkout's. The foreign fixture records the other
  task's worktree at a path the manifest still enumerates — see the note below.
- `test_worker_launch.py` — `RecordedWorktreeExecutionTests` (AC-9),
  `LegacyCheckoutExecutionTests` (AC-10), plus stale/blank recorded-worktree
  fallback and `worker_env` with no task state. The acceptance half is not
  mocked: a criterion prints `os.getcwd()` and the test compares it to the
  expected root.
- `test_context_bus.py` — `WorktreeHookBehaviourTests` (AC-27),
  `WorktreeHookCopyAuthorityTests` (AC-31). AC-31 copies the real hooks into
  both trees with a distinguishing stderr marker and launches the **registered
  cwd-relative command** from `.claude/settings.json`, asserting the worktree
  copy executed while the blackboard, constitution and Stop evidence came from
  the checkout. The worktree also holds a decoy `context.jsonl`,
  `implementation.md` and `constitution.md`, so a cwd-resolving hook would fail
  the test rather than pass it silently.
- `test_hardening.py` — `ProtectedCritiqueEvidenceTests` (AC-23, AC-26),
  `WorktreePreToolUseTests` (AC-28). AC-26 parses CLAUDE.md's
  orchestrator-owned list, translates the documented
  `critique-<plan-stem>-round-<round>.json` convention into a matcher, and
  compares it against the guard's own pattern over a sample of family and
  near-miss paths — not a substring grep. AC-28 runs the worktree's copy of the
  guard against a decoy plan in the worktree and asserts the checkout's
  approved plan is the one that authorises.
- `test_replan_resume.py` — `PersistedCritiqueTests` (AC-12, AC-21, AC-22),
  driven through `run_plan`'s real rounds-exhausted control flow.
- `test_plan_contract.py` — `InitialPlanningFailureTests` (AC-14), with a
  stubbed planner that exits non-zero and a `FileNotFoundError` variant, plus a
  test that the resulting `FAILED` state really routes.
- `test_fix_loop.py` — `FailureReasonLifecycleTests` (AC-13). The exits are
  enumerated against `orch.FAILURE_ROUTES`, so a route added later fails this
  test rather than slipping past; `DEVELOPER_CLARIFICATION` is asserted to
  *keep* the reason, because it is not an exit.
- `test_repo_hygiene.py` — `CiMatrixTests` (AC-15) plus a narrow hand-written
  standard-library workflow reader (`parse_workflow` /
  `matrix_python_versions`). It parses the matrix structure, raises
  `WorkflowError` on absent, empty, tab-indented, non-mapping, unterminated or
  structurally unrecognised input, and is exercised against ten such inputs so
  "cannot parse" can never read as a pass. A block-list reformat of the same
  matrix is asserted to still pass.

## Deviations from the approved plan

None in scope, files or design. Two things worth flagging as judgement calls
inside it:

- AC-7's fixture records the second task's worktree at `sandboxes/TASK-998`
  rather than `.worktrees/TASK-998`. `BASELINE_EXCLUDE_PREFIXES` drops
  `.worktrees/` and `.ai/tasks/` from the manifest outright, so a fixture in the
  conventional location would have produced an empty delta and proved nothing
  about classification. The `.ai/tasks/<other-id>/` half of structural ownership
  is asserted through `classify_foreign_paths` directly for the same reason.
- The plan's residual F2 case is asserted rather than only documented
  (`test_another_tasks_plan_is_never_read`): a concurrent open task with no
  recorded worktree that edits shared source still lands in this task's delta
  as a violation. That is the accepted trade from decision 2, and it now fails
  loudly if someone "fixes" it by reading another task's plan.

## Verification actually run

Commands run from the TASK-008 worktree with `python` = the Python 3.13
interpreter the orchestrator uses.

- Full discovery, AC-16's own command: **722 tests, OK** (baseline was 659; the
  truncated run had 684 with two failures).
- Every acceptance criterion's `verify` command executed individually: AC-1
  through AC-15 and AC-17 through AC-32 all exit 0. AC-16 was run separately as
  above.
- `.ai/validation.json`'s other checks: `py_compile` on both scripts, and
  `json.tool` on the task-state and plan schemas — all pass. I also compiled the
  four hooks and ran `bash -n .ai/hooks/run`, since three hooks changed.
- Task delta against `baseline.json`, computed read-only: exactly the 14
  declared files modified, `created`/`deleted` empty, `violations` empty,
  `foreign` empty, `unproduced` empty. The four compatibility modules the plan
  deliberately does not declare (`test_preflight.py`, `test_worktree_pr.py`,
  `test_validation_profile.py`, `test_review_package.py`) are byte-identical and
  AC-32 runs them directly.

## One thing the next worker needs to know

A suite run *inside* an orchestrated worker session inherits that session's
`ORCHESTRATOR_REPO_ROOT`, `ORCHESTRATOR_TASK_DIR`, `ORCHESTRATOR_WORKER_ROOT`
and `ORCHESTRATOR_CONSTITUTION`. The hooks now read those exports by design, so
existing tests that launch a hook with a fixture cwd and an inherited
environment (`test_plan_authorization.py`, `test_hardening.py`'s `run_hook`,
`test_context_bus.py`'s `HookBehaviourTests`) resolve the *real* TASK-008
evidence instead of their fixture's: 14 extra failures appear, including
`test_undeclared_script_is_denied`, because the real approved plan declares
`.ai/scripts/orchestrator.py`.

This is environment leakage into the test process, not a defect in the guard,
and it does not affect validation: the orchestrator runs validation from its own
environment, which has none of those variables. Cleared of them, the suite is
green — that is how the 722-test run above was executed, and it matches the
684/2 figure ctx-005 recorded. Every test I added builds its subprocess
environment explicitly, stripping `ORCHESTRATOR_*`, so the new coverage is
immune. I did not change the older modules, because the plan does not declare
them and an undeclared write is denied at the tool boundary. If they are ever
touched, the fix is to scrub the environment there too — not to weaken the
hooks' exported-root resolution, which is the single-homing guarantee AC-27,
AC-28 and AC-31 exist to hold.

---

# Fix pass 3 — the planner half of the high finding, and its test coverage

Entry: routed `CLAUDE_FIX` after fix pass 2 was truncated by the worker session
quota (ctx-010). No files beyond the approved 14 were touched.

## What changed

**`.ai/scripts/orchestrator.py` — `codex_argv`.** `--output-last-message` is now
`str(authoritative_path(plan_file))`. It was the relative
`.ai/tasks/<id>/plan.json` while `run_worker` launches codex with
`cwd=execution_root(task_id)`, so for a task with a recorded worktree the
planner could write its plan into the worktree while the caller then checked
`plan_file.is_file()` in the checkout. This is the same treatment
`run_claude_implementation` and `run_claude_fix` already had; putting it inside
`codex_argv` covers both callers at one place. The docstring says why.

`run_codex_planning` and `run_codex_replan` no longer bind a local name
`repo_root = Path.cwd()`, which shadowed the module's own `repo_root()`; they
call `repo_root()` and pass it as `checkout`. `--cd`, `--output-schema` and the
returned/recorded `plan_file` are unchanged: the plan path recorded in
`events.jsonl` and `state.json` stays repo-relative so evidence keeps comparing
across machines, and only the path handed to the subprocess is absolute.

**`.ai/scripts/orchestrator.py` — `run_recover`'s lock handling.** `holder is
not None` was doing duty as "a lock existed", and `lock_holder` returns None for
a zero-byte or unreadable lock. `task_lock` creates the file with
`O_CREAT|O_EXCL` and writes the pid line afterwards, so a process killed between
the two leaves exactly that lock — the interruption this verb exists for. With
such a lock and an override supplied, no `RECOVERY_LOCK_OVERRIDDEN` event was
written, `IMPLEMENTATION_INTERRUPTED` recorded `lock_overridden=False`, and the
file was never unlinked, so `route-failure` then refused and the task was
stranded again. Existence is now the flag (`locked`), and the holder stays what
it always was: advisory detail, recorded as `"unknown"` when the file says
nothing.

The unlink is no longer swallowed under `contextlib.suppress(OSError)` with
"Removing …" printed beforehand. Removal is attempted first, `Removed …` is
printed only when it succeeded, an `OSError` is reported with what it means for
the next verb, and `RECOVERY_LOCK_OVERRIDDEN` now carries `lock_removed` so the
trail records what happened rather than what was intended.

**`test_worker_launch.py` — `WorktreeEvidencePathTests` (5 tests).** The gap the
reviewer named: AC-9 asserted only subprocess cwd and exported env vars, so
nothing covered what a worktree-rooted worker is *told* to read and write. The
class asserts, with a recorded worktree in place and cwd verified to be it, that
the implement prompt, the fix prompt, the planner's `--output-last-message` and
the replan's all name the checkout's evidence. `assertEvidenceIsCheckoutRooted`
compares occurrence *counts* of the bare filename and of the absolute path, so a
second relative mention elsewhere in a prompt fails it; each test also asserts
the worktree path appears nowhere in the prompt. One test pins the legacy
null-worktree case, where both roots coincide.

**`test_run_driver.py` — two tests in `RecoverLockSafetyTests`.**
`test_an_empty_lock_is_still_a_lock` recovers past a zero-byte lock and asserts
the three things the defect silently skipped: the override event exists with its
human attribution, `IMPLEMENTATION_INTERRUPTED.lock_overridden` is true, and the
file is gone so the next verb can take a lock.
`test_a_lock_that_cannot_be_removed_is_reported_not_swallowed` makes `unlink`
raise `PermissionError` and asserts the developer is told it could not be
removed, is not told it was, and that `lock_removed` is false.

## Deviations

None in scope, files or design. Two judgement calls:

- The reviewer also noted `--cd` for the planner is the orchestrator checkout
  while `run_worker`'s cwd is the recorded worktree, so a planner run for a task
  with a worktree analyses the checkout. I did not change that. It is not a
  regression (before worktree rooting both were the checkout), no acceptance
  criterion covers it, and picking a different `--cd` would also move
  `--output-schema` onto the worktree's copy of the schema. Recording it here
  because it is a real inconsistency someone should decide on deliberately.
- The empty/unreadable-lock defect is a `medium`, not the routed `high`. I fixed
  it because it is in this task's own new code, in an already-declared file, and
  its effect is to strand a task behind an unclaimed lock — the exact failure
  `recover` exists to remove.

## Verification actually run

From the TASK-008 worktree, with `ORCHESTRATOR_REPO_ROOT`,
`ORCHESTRATOR_TASK_DIR`, `ORCHESTRATOR_WORKER_ROOT`, `ORCHESTRATOR_CONSTITUTION`
and `ORCHESTRATOR_TASK_ID` cleared — the environment the orchestrator validates
from, for the reason recorded above.

- AC-16's own command: **discovered 729, Ran 729 tests, OK** (722 before this
  pass; +5 in `test_worker_launch.py`, +2 in `test_run_driver.py`).
- Targeted re-run of the recovery, worktree-execution and new path classes: 30
  tests, OK. `py_compile` on `.ai/scripts/orchestrator.py` passes.
- **Counterfactual, so the new tests are not decoration.** With
  `orch.authoritative_path` patched to the identity (and `evidence_dir` to
  `task_dir`), reproducing the pre-fix behaviour, all four prompt/planner tests
  fail — the two planner tests on `out.is_absolute()`, the two prompt tests on
  the absolute path being absent. I did not run a counterfactual for the
  empty-lock tests; that defect was established by reading the code path
  (`holder = lock_holder(...)` → None → the `if holder is not None:` block is
  skipped entirely), and the test asserts each of its three consequences.
- Task delta against `baseline.json`, computed read-only through
  `compute_task_delta`: the same 14 declared files modified, `created` and
  `deleted` empty. My own edits in this pass were `.ai/scripts/orchestrator.py`,
  `test_worker_launch.py` and `test_run_driver.py`.

# Fix pass 4 — AC-16's 300s acceptance budget

The routed failure was AC-16 alone: `returncode 124, timed out after 300s`,
while the same suite passed as the `tests` validation check in 232.9s under the
larger `VALIDATION_TIMEOUT_S`. So the defect to fix was suite wall time, not
behaviour. I did not narrow AC-16, delete the duplicate `tests` check, edit
`.ai/validation.json` or remove a test; the criterion still runs full discovery
and still requires a non-empty successful suite.

## What the measurements said before I changed anything

All figures on this host, `ORCHESTRATOR_*` cleared, wall time of full
discovery:

- **107.3s / 729 tests** at the failing state.
- **92.2s / 659 tests** at `fd35909` — measured by cloning this worktree into a
  temp directory and checking out the baseline commit, so the comparison is the
  same host and the same interpreter. The suite was already within a third of
  the 300s cap *before* this task; this task's ~15s of new subprocess-heavy
  tests is what tipped it over on a loaded host.
- Per-test-class profiling of the failing state attributes the +15s to the new
  test classes, not to slower orchestrator code: pre-existing
  `test_task_baseline` classes cost 27.5s against 29.0s at the baseline commit,
  and pre-existing `test_hardening` classes 2.9s against 2.8s. **There is no
  measured runtime regression in `.ai/scripts/orchestrator.py`.**
- Instrumenting `subprocess.run` across the suite: 67.5s of 70.5s in the two
  slowest modules was subprocess time, dominated by per-test-method git
  repository construction (`init`, two `config`, `add`, `commit`, `checkout`),
  ~0.4s per test method on Windows.

## What I changed

**`test_task_baseline.py` — `BaselineCase` builds its starting repo once.**
The fixture every test in the module begins from is identical each time, so it
is now built once per task id into a temp *template* (`_build_starting_repo`,
`_template_repo`) and copied into each test's own tree in `setUp`; a module
`tearDownModule` removes the templates. Measured on this host: 0.400s to build
against 0.051s to copy. `_profile` became a thin call on a module-level
`_write_profile(root, count)` so the template and the tests that re-write the
profile with a different expected count share one definition, and `_git`
delegates to a module-level `_git_in` so tests that make their own commits
(`CommittedEvidenceTests`) or add a worktree (`RecordedWorktreeDeltaTests`) are
unchanged.

Equivalence checked directly rather than assumed: in a copied fixture,
`git branch --show-current` is `feature/TASK-999`, `git status --porcelain` and
`git diff --name-status` are empty, `git log --oneline` is the single `base`
commit, and `git ls-files` is exactly `.ai/validation.json` and `base.txt` —
the same starting state the six subprocesses produced. Module time 35.4s →
21.4s, all 63 tests still pass, no assertion touched.

**`test_repo_hygiene.py` — one `git check-ignore` for a set of paths.**
`test_no_currently_tracked_file_is_ignored` ran `git check-ignore` once per
tracked file (~90 processes, 3.5s of the module's 5.5s). New `ignored_among()`
asks the same tool the same question for the whole set via `--stdin -z` and
returns the paths git reported as ignored; `is_ignored()` is now a one-element
call on it, so both directions still go through git rather than a
reimplementation of its matching rules. The four `TrackedArtifactTests` methods
that looped over path lists now assert on the returned offender list, which
names the offending paths in the failure message as the per-path assertion did.
Module time 5.5s → 0.8s.

Two details worth keeping:

- `text=True` rewrites the separator to CRLF on Windows, and git then treats
  the CR as part of the path and quotes it (`".venv/bin/python\r"`), so nothing
  matches and every "is ignored" assertion silently inverts. I hit exactly that
  — 8 failures in `IgnoredArtifactTests` — and fixed it by writing bytes with
  `-z`. The comment in the helper records it.
- A `returncode` other than 0 or 1 raises rather than returning an empty list:
  git failing to answer must not read as "nothing is ignored".

## Result

Full discovery, `ORCHESTRATOR_*` cleared, on this host:

- **AC-16's own verify command: `discovered 729`, `Ran 729 tests`, `OK`.**
- Four post-fix full runs: **82.6s, 84.0s (AC-16's own command), 98.8s and
  150.8s (AC-16's own command again)**. All green. Pre-fix on the same host:
  107.3s and 107.5s. So the fix removes ~20-25s reliably, and the spread
  between runs minutes apart is larger than the saving — the 150.8s run is the
  same bytes as the 84.0s one. I am reporting the whole set rather than the
  best draw, because the failure being fixed was a timeout and the distribution
  is the thing that matters.
- Median is now faster than the pre-task baseline (92.2s at `fd35909`) while
  the suite carries 70 more tests.
- Profiling the 98.8s run: `test_review_package` 42.9s and
  `test_validation_profile` 6.7s are the two largest remaining modules and both
  are undeclared and byte-identical by constraint, so ~50s of the suite is
  outside anything this task may touch.
- AC-32's four undeclared compatibility modules: 140 tests, OK, and they remain
  byte-identical — the delta lists 14 modified files, `created` and `deleted`
  empty, so my edits in this pass (`test_task_baseline.py`,
  `test_repo_hygiene.py`) stayed inside the declared set.
- AC-15 and the `test_task_baseline` criteria AC-7/AC-8/AC-9/AC-11 re-run
  individually: OK.

## What I did not do, and the decision it leaves open

I did **not** raise `ACCEPTANCE_TIMEOUT_S` (300s, `orchestrator.py:23`).
ctx-015 records that as a workflow-wide behaviour change affecting every task's
acceptance commands and not a worker's decision to take alone, and I agree with
that reading, so the constant is untouched.

What the developer should know when deciding whether it still needs raising,
stated plainly because it bears on whether this fix is sufficient:

- The validating host measured this suite at 232.9s where this session measures
  a ~90s median for the same bytes (~2.2x). Applying that factor to the new
  median puts AC-16 near 200s against the 300s cap.
- But the spread is not only across hosts. On *this* host, minutes apart and
  with identical bytes, the same command took 84.0s and 150.8s. A comparable
  bad draw on a 2.2x-slower host would exceed 300s.
- So this pass reduces the probability of the timeout; it does not make AC-16
  structurally safe. The remaining ~50s of the suite lives in modules the plan
  forbids this task from touching, so the worker-side lever is close to
  exhausted. If AC-16 times out again, the honest options are the repo-wide
  `ACCEPTANCE_TIMEOUT_S` raise (which does not weaken the criterion: it still
  requires the full non-empty suite to pass) or removing the duplication AC-16
  has with the `tests` check — both developer decisions, not mine.
- The duplication itself — the same discovery run once under
  `VALIDATION_TIMEOUT_S` and once under `ACCEPTANCE_TIMEOUT_S` — is in
  plan-v4's own risk list and is unchanged by this pass.

Also unchanged, and worth one line because I looked at it: four live
`claude --print --agent failure-classifier` invocations happen during the suite
(1.7s total here) from `route_failure` tests that do not set
`ORCHESTRATOR_DISABLE_CLASSIFIER=1`. They are pre-existing, they fall back
deterministically, and their latency is external. I left them alone rather than
change what a pre-existing test exercises for ~2s, but they are the one part of
the suite whose duration this repository does not control.
