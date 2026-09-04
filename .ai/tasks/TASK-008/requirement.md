# TASK-008 — Make an interrupted task recoverable and a task delta trustworthy

Six defects in the lifecycle and provenance machinery added by `679d5c9` and
`811626d`. Five were found by running TASK-007 through the pipeline; the sixth
was found by running TASK-008's own planning round.

## Execution context — read this before planning

This task's lifecycle runs with the working directory set to
`.worktrees/TASK-008`, a git worktree on branch `feature/TASK-008` at
`fd35909`. That is deliberate, and it is the reason plan v1 was rejected.

- **Every declared path resolves inside this working tree.** Declare
  `.ai/scripts/orchestrator.py`, not `.worktrees/TASK-008/.ai/scripts/orchestrator.py`.
  A plan that declares a `.worktrees/` path is wrong: the implementer runs here,
  so `.worktrees/` is not a prefix it ever needs.
- **The main checkout is frozen and must not be touched.** TASK-007 has an open
  whole-tree baseline there, so any edit to a non-allowlisted file in the main
  checkout would register as a TASK-007 scope violation. Isolation is achieved
  by *where the orchestrator runs*, not by code this task has yet to write.
- **This tree now contains `fd35909`**, which committed the previously
  uncommitted orchestrator work: `fix_authorization`, `replan_budget`, the
  plan-schema `required` fix and their tests. That code is **present** and is
  not this task's subject matter — do not revert, re-derive or "fix" it. In
  particular `fix_authorization` sits next to `route_failure` and `run_fix`,
  which F1's `recover` verb also touches, so extend that code rather than
  replacing it.
- The `.ai/tasks/TASK-007/` directory in this tree is an **empty placeholder**
  with no state, created only so `allocate_task_id()` would not reuse a live
  task id. It is not task evidence. Ignore it.

## Problem

**An interrupted `implement` strands the task with no legal verb.** TASK-007's
implementer was killed on 2026-09-02 at ~20:21, after editing both declared
files and running tests, but before the orchestrator recorded an outcome. The
task sits in `IMPLEMENTING` with an `IMPLEMENTATION_STARTED` event and no
terminating event. Every verb refuses: `implement` requires
`AWAITING_APPROVAL`, `validate` requires `VALIDATING`, `route-failure` requires
`FAILED`, and `fix` requires a `CLAUDE_FIX_STARTED` event that only
`route-failure` can emit. `run` detects the case and stops. The task can only
be moved by hand-editing `state.json` — verbatim the defect `run_fix`'s own
docstring says it was written to remove, closed for the route-failure entry
into `IMPLEMENTING` and left open for the interrupted-implement entry.

**A task delta is a whole-tree diff, so unrelated work becomes a scope
violation.** `baseline.json` manifests the whole tree, `compute_task_delta`
compares the current tree against it, and `evaluate_diff_scope` reports every
changed path that is neither declared nor in `DIFF_SCOPE_ALLOWLIST` (only
`.ai/tasks/` and `.ai/scripts/__pycache__/`). So a task's delta grows for as
long as the task is open, two tasks cannot be in flight at once, and an
interrupted task makes the tree unusable for other work.

**`committed_changed` reports accumulated branch history as task evidence.**
`resolve_diff_base` returns merge-base(`origin/main`|`main`, HEAD). On a
feature branch several commits ahead that is the fork point, so
`committed_changed` lists everything on the branch — 74 files for TASK-007 —
none of which the current task produced. Non-blocking, but it is noise shaped
like evidence.

**The `worktree` verb is decorative.** `run_worktree` creates the tree and
records `state["worktree"]`; nothing reads it as a working directory, because
every subprocess runs in `Path.cwd()`. The stated purpose — "what lets tasks
run in parallel without fighting over the checkout" — is not delivered.

**A stale `failure_reason` persists on a healthy task.** TASK-007's
`state.json` still carries `"failure_reason": "fix-precondition: plan_rejected"`
from a superseded routing decision while its status is `IMPLEMENTING` and its
approval gate passed.

**A plan critique is counted but never recorded.** `critique_round` calls
`record_event(..., issues=len(...), blocking=len(...))` — counts only. The
issue text goes to stdout and nothing persists it; there is no critique file in
the task directory. Yet on exhausting its rounds the orchestrator prints
"Presenting the plan to the developer with the critique recorded; read it
before approving." It is counted, not recorded. A developer at the approval
gate is told two blocking issues exist with no way to read them. This was found
on TASK-008's own plan v1, whose critique had to be recovered from a transient
524 KB log.

**A failed planning run is unhandled.** `run_plan` lets the worker's
`subprocess.CalledProcessError` escape as a raw traceback. The task is left in
`PLANNING` with `plan_file: null` and **no `PLAN_FAILED` event**, so nothing in
the task trail records that planning was attempted and failed — only a
`WORKER_USAGE` entry with a short duration hints at it. `run_replan` records
`REPLAN_FAILED` for the identical failure mode, so this is the same asymmetry
as F1: handled on one entry point, unhandled on the other. Observed on
TASK-008's own planning run, which died in an HTTP 400 from the planner.

## Scope

| # | Finding | Site |
|---|---------|------|
| F1 | Interrupted `IMPLEMENTING` has no legal exit | `.ai/scripts/orchestrator.py` |
| F2 | Whole-tree delta makes unrelated work a scope violation | `.ai/scripts/orchestrator.py` |
| F3 | `committed_changed` sources unmerged branch history | `.ai/scripts/orchestrator.py` |
| F4 | `worktree` recorded but never used as a working directory | `.ai/scripts/orchestrator.py` |
| F5 | `failure_reason` survives after the failure is resolved | `.ai/scripts/orchestrator.py` |
| F6 | Plan critique text is never persisted, only counted | `.ai/scripts/orchestrator.py` |
| F7 | A failed planning run raises a traceback and records no `PLAN_FAILED` | `.ai/scripts/orchestrator.py` |

## Decisions already taken (do not re-open)

Answered by the developer during clarification; the plan must follow them.

1. **F4 becomes real**, not opt-in and not removed: the recorded worktree
   becomes the working directory for that task's worker execution and delta
   computation. An opt-in flag was rejected for leaving the default path
   defective.
2. **F2 uses a non-blocking `foreign` list**: keep the whole-tree delta and
   demote paths belonging to other open tasks to a recorded, non-blocking list.
   Do **not** subtract paths declared by other tasks' approved plans — that
   would make one task's plan an authorisation input for another's validation.
3. **F1 gets a new explicit `recover` verb.** `run` keeps its deliberate stop
   and names the verb in its diagnostic. No autonomous self-healing.
4. **Recovery is fail-closed on the lock**: refuse whenever a `.lock` exists
   unless the developer supplies an explicit override, recorded in the event as
   an attributable human assertion. A pid probe must not be the gate —
   `os.kill(pid, 0)` is unsafe on Windows, where Python maps a non-CTRL signal
   to `TerminateProcess`. Advisory pid detail inside the event is welcome.

## Out of scope

- The `.ai/tasks/TASK-001/` pollution artifact in the main checkout — a test
  fixture calling `record_event` against the real repository. Real, separate,
  needs its own task. Do not delete it.
- TASK-007's recovery. This task delivers the mechanism; applying it is a
  separate developer decision.
- Merging or reconciling the main checkout's uncommitted work.
- Any change to the encoding behaviour TASK-006 and TASK-007 established.
- Re-capturing any existing baseline. Write-once stays.

## Acceptance criteria

Each must be executable, must use only `{python}`, and must assert observable
behaviour rather than the existence of a test the implementer itself authors.

1. A task in `IMPLEMENTING` whose run was interrupted reaches a routable state
   through the `recover` verb with no hand edit to `state.json`. Verified
   against a constructed state: `IMPLEMENTATION_STARTED` present, no
   terminating event, no `CLAUDE_FIX_STARTED`.
2. Recovery records the interruption as an event carrying its reason and the
   asserting developer. Assert the event and its fields, not just the status.
3. Recovery is fail-closed and never manufactures completion: it refuses while
   a `.lock` exists without an explicit override, records the override as a
   human assertion when given, and a recovered task cannot reach `VALIDATED`
   without validation having run.
4. `run`'s interrupted-`IMPLEMENTING` diagnostic names the `recover` verb, and
   `run` still refuses to proceed on its own.
5. Concurrent tasks do not contaminate each other's scope: with two tasks open,
   changes to task B's declared files appear in task A's result as a recorded
   non-blocking `foreign` entry and not as a violation.
6. Unmerged branch history is not presented as current-task evidence, on a
   repository whose base is several commits behind.
7. Worker execution and delta computation are rooted in the task's recorded
   worktree when one is present.
8. A task with **no** recorded `state["worktree"]` — every task that exists
   today, including TASK-007 — remains drivable and its evidence readable.
9. A plan critique's issue text is persisted to the task directory, with
   severities, and is readable without consulting process output. Assert the
   content of what was written, not that a file exists.
10. `failure_reason` does not survive a task leaving `FAILED`.
11. A planning run whose worker exits non-zero records a `PLAN_FAILED` event
    carrying the reason and leaves the task in a state the developer can act
    on, instead of raising a traceback. Verified with a stubbed worker that
    exits non-zero; assert the event and the resulting state, not the absence
    of an exception alone.
12. The CI matrix still runs Python 3.9 and 3.12. Parse the workflow rather
    than matching an exact literal, so a cosmetic reformat does not fail it.
    Deliberate regression guard.
13. The full suite is non-empty and passes under the validation interpreter.
    Deliberate regression guard.

## Criteria that must also exist (added after plan v1's critique)

Plan v1 satisfied the list above while leaving these holes. The plan critic
called the first one blocking, twice. Every item here needs its own executable
criterion; do not fold them into a criterion that already passes.

14. **`recover` refuses an ineligible task.** Separate criteria, each
    exercising a refusal: a task not in `IMPLEMENTING`; a task in
    `IMPLEMENTING` that already has a terminating event; a task that already
    has `CLAUDE_FIX_STARTED`. Without these, `recover` can be implemented as an
    unconditional status setter and every other criterion still passes — which
    is precisely the hand-edit defect the verb exists to remove.
15. **Interrupted work on disk survives recovery.** Assert the bytes of the
    interrupted implementer's edits are unchanged after `recover` runs.
    "Preserves all work on disk" is currently prose only, so a recovery that
    reset or re-captured files would pass everything.
16. **The rounds-exhausted blocking critique is the one persisted.** F6's
    actual defect is the critique shown at the approval gate when the critic
    still objects. Assert that specific artifact exists with its issue text and
    severities, and that it identifies its round and plan version so a stale
    artifact cannot be mistaken for the current one.
17. **At least one criterion drives `orchestrator.py` as a subprocess** through
    real CLI dispatch. Every criterion in plan v1 was a unittest the
    implementer also authored, and a unit test that patches internals can pass
    while the CLI path is broken — the failure mode that hid four separate
    defects before `preflight` existed.
18. **`CLAUDE.md` is declared and updated.** Adding a verb leaves its verb list
    and its "`IMPLEMENTING` has exactly one exit" narrative false. A documented
    contract that contradicts the code is a defect, not a leftover.

## Criteria that must also exist (added after plan v2's critique)

Plan v2 passed the critic with these left open. They are not polish.

19. **The null-worktree launch path is tested.** Assert that worker and
    acceptance subprocess launching falls back to the orchestrator checkout
    when `state["worktree"]` is null — not merely that baseline and delta
    evidence stay readable. Every task that exists today runs this path,
    **including TASK-008 itself**, whose own `state.json` has
    `"worktree": null`. A regression here would break the task doing the work.
20. **The route to `VALIDATED` still requires validation.** Assert the
    post-recovery route, not `recover`'s silence. "Recovery emits no validation
    event" does not settle that a recovered task cannot reach `VALIDATED`
    without validation having run, which is what the guarantee says.
21. **The persisted critique artifact is orchestrator-owned, enforceably.** Add
    it to `PROTECTED_EVIDENCE_PATTERNS` in `.ai/hooks/pre_tool_use.py` and to
    `CLAUDE.md`'s orchestrator-owned artifact list, and assert that a worker
    write to it is **denied at the tool boundary**. Calling it
    orchestrator-owned in a plan constraint while the guard does not list it
    would ship a new evidence file a worker can write its own version of —
    against the rule that no plan can authorise a worker to write its own
    verdict.
22. **The critique artifact has a declared path and name.** State where it
    lives and how it is named, and assert that a stale round's artifact is
    distinguishable from the current one. A developer told to read it at the
    approval gate must not have to read the implementation to find it.
23. **Unbundle the recovery-safety criteria.** Lock refusal, override
    attribution and the no-manufactured-completion guarantee are three
    separable obligations; one implementer-authored test method covering all
    three can pass while any one of them is unimplemented.

Also required, as a statement rather than a test: **surface F2's residual
case.** Structural ownership (another task's recorded worktree, or its
`.ai/tasks/<id>/`) leaves one case unfixed — a concurrent task with *no*
recorded worktree that edits shared source files still lands in task A's delta
as a blocking violation. That narrowing follows from decision 2 and is
acceptable, but it must be written into the plan's risks so it is accepted
knowingly rather than discovered later.

## Criteria that must also exist (added after plan v3's critique)

Plan v3 passed the critic. These four are developer-mandated closures of its
remaining issues. Each needs its own executable criterion.

24. **A dead PID does not authorize recovery.** A lock whose recorded pid is
    demonstrably not running must **still** refuse recovery when no override is
    supplied. AC-3's statement claims "without using a PID liveness probe as
    authorization" while its command only tests refusal in general, so an
    implementation that probes `os.kill(pid, 0)` and proceeds when the pid looks
    dead would satisfy it and violate decision 4. Assert the dead-pid case
    directly. Pid information may appear in the event as advisory detail only.
25. **Recovery metadata is schema-valid.** Constraint 3 keeps recovery identity
    and timestamps out of `state.json` because
    `.ai/schemas/task-state.schema.json` sets `additionalProperties: false`.
    Verify it: validate the post-recovery `state.json` against that schema and
    assert it passes. A documented decision that nothing checks is how
    `recovered_at` ships and state validation fails a week later.
26. **Which copy of the hooks executes is established and proven.**
    `.claude/settings.json` registers hooks as cwd-relative commands
    (`bash .ai/hooks/run .ai/hooks/session_start.py`), so once a worker's cwd is
    the recorded worktree, the scripts that run are **that worktree's copies**,
    not the orchestrator checkout's. Decide explicitly which copy is
    authoritative, state it in the plan, and add a criterion that proves the
    intended copy is the one that executed — for example by making the two
    copies distinguishable in the fixture and asserting which one ran. AC-27 and
    AC-28 currently pass either way, which means neither settles it.
27. **The declared file list must not be able to strand the implementer.**
    Rooting subprocesses at the recorded worktree most plausibly changes
    `worker_env()`, and these existing modules exercise that or cwd/merge-base
    behaviour while being undeclared: `test_preflight.py` (which calls
    `orch.worker_env("TASK-001")` for a task with no state or worktree),
    `test_worktree_pr.py`, `test_validation_profile.py`,
    `test_review_package.py`. AC-16 requires the whole suite to pass, so a
    behaviour change that breaks any of them leaves the implementer unable to
    fix it (undeclared write, denied at the tool boundary) and unable to leave
    it (criterion fails). Resolve it one of two ways, and say which:
    - **Preferred:** design the change to be backward compatible so those
      modules genuinely do not need to change, and state in the plan why each
      is unaffected.
    - **Otherwise:** declare the ones that must change — but only those, since a
      declared file left byte-identical to the baseline fails validation as
      declared-but-unchanged.

    Do not leave a third path where a test fails and nobody may touch it.

Issue 4 from the v3 critique — that worktree-rooted execution with single-homed
evidence is a decision outliving this task and belongs beside ADR-0003 — may
remain a documented non-blocking risk. Write the ADR only if it falls out of the
work naturally; do not expand scope for it.

## Constraints

- Standard library only.
- **No criterion may rest on a threshold the implementer chooses.** An
  anti-weakening guard whose baseline test count is a literal the author picks
  can be satisfied by picking a low one. Derive the comparison from something
  the implementer does not author, or drop the criterion rather than ship one
  that cannot fail.
- **A criterion must fail, not skip, when its input is missing.** The CI-matrix
  check must fail if `.github/workflows/ci.yml` is absent or unparsable. Under
  the stdlib-only rule that means a hand-rolled reader narrow enough to be
  correct — say so in the test strategy, and do not let "cannot parse" become a
  pass.
- **Do not declare a file you will not change.** `_support.py` was declared in
  plan v1 while its fixture work could plausibly be written inline in the test
  modules, which would leave it byte-identical to the baseline and fail
  validation as declared-but-unchanged. Declare it only if it genuinely changes.
- Backward compatible. Existing task evidence stays readable, and any change to
  the `baseline.json` shape must tolerate manifests already on disk.
- `.ai/schemas/task-state.schema.json` sets `additionalProperties: false`. Any
  new field written to `state.json` — a recovery timestamp, an override
  asserter — fails state validation unless that schema is also changed, so
  either declare it in `files_to_modify` or keep recovery metadata in
  `events.jsonl` only. Decide explicitly; do not leave it to chance.
- If worker execution becomes worktree-rooted, `.ai/hooks/session_start.py` and
  `.ai/hooks/stop_guard.py` are part of that change and must be declared: both
  resolve `.ai/tasks/<id>` and `.ai/constitution.md` relative to cwd, so a
  worker rooted elsewhere would read the wrong blackboard and check the Stop
  guard against the wrong evidence. An undeclared write to either is denied at
  the tool boundary.
- Do not weaken or delete an existing test. The suite that passes today must
  still pass.
- Fail closed. A recovery path that cannot establish what happened refuses
  rather than guessing, and never records a pass it did not observe.
- Declare every file to be touched, including anything under `.ai/scripts/`,
  `.ai/schemas/` and `.ai/hooks/`; the `PreToolUse` guard checks declarations
  against the approved plan's bytes.
- Every requirement in the plan needs an acceptance criterion. Plan v1 was
  criticised for carrying a requirement that appeared only as prose in a file
  purpose, so nothing executed settled it.
- A criterion that verifies documentation must not be a substring grep: the
  word "recover" already appears in `orchestrator.py` as "recoverable" and
  "recovered", so a grep is satisfied by an incidental mention. Assert
  dispatch behaviour instead.
- Acceptance commands may use only the `{python}` interpreter. Multi-version
  runtime coverage belongs to the CI matrix.
