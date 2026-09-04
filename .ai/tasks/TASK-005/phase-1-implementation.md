# TASK-005 — Phase 1: Real validation

Implements Phase 1 of `phased-plan.md`. Branch `feature/TASK-005`. Not
committed — the developer reviews the diff.

Phase 0 made the existing gates *honest*. Phase 1 makes them *mean something*:
the plan becomes a parsed contract whose declarations are enforced, and
validation becomes a profile of independently-recorded checks rather than one
exit code.

## Verification status

**204 tests, all passing.** (Phase 0 ended at 84.)

All three CI steps pass on every supported interpreter:

| Version | Platform | Result |
|---|---|---|
| **3.9.25** | Windows native | 204 tests, exit 0 |
| **3.12.13** | Windows native | 204 tests, exit 0 |
| 3.14.4 | WSL Ubuntu | 204 tests, exit 0 (1 skip: Windows-only path test) |

Beyond the unit suite, the pipeline was exercised **end to end in throwaway git
repositories**, with no mocks: real plan parsing, real `verify` commands, real
`git merge-base` diffing. Two runs, both behaving correctly —

- criteria executed (`AC-1` pass, `AC-2` pass via a real import, `AC-3`
  `judge` → unverified), empty suite correctly failing the task;
- diff-scope catching two undeclared files (`sneaky.py`, `test_widget.py`) and
  failing validation while every other check passed.

## Decisions

The developer chose **plan → JSON** and directed that the migration leave no
stale references. Two follow-on choices were mine:

1. **`.ai/validation.json`, not `.ai/validation.yaml`.** The audit names the
   file `.yaml`; using YAML would reintroduce the dependency the JSON decision
   just removed. Same reasoning, same answer.
2. **No custom YAML parser**, per instruction. Legacy `.yaml` plans are
   therefore *unparseable by design*: they get the Phase 0 structural pre-flight
   and are reported as skipped-with-a-reason wherever parsing is required,
   rather than silently reporting zero criteria.

## What changed

### 1 — Plan is JSON, parsed and schema-driven

- `plan.json` / `plan-vN.json` replace `plan.yaml` / `plan-vN.yaml`.
- `.ai/schemas/plan.schema.json` is new, and does double duty: passed to
  `codex exec --output-schema`, and the reference for `validate_plan()`.
- `load_plan()` is a real `json.loads` parse. `validate_plan()` enforces the
  parts the orchestrator consumes — required keys, container types, per-criterion
  `id`/`statement`/`verify`, unique ids, resolvable file paths.
- The planning prompt was rewritten to demand a single JSON object, explain that
  `verify` commands are executed, and warn that file lists are enforced.

**Deliberate duplication.** Validating against the schema file itself would need
a `jsonschema` dependency, so `validate_plan()` is hand-rolled. A test asserts
the schema's `required` list and the orchestrator's `PLAN_REQUIRED_KEYS` match,
so the two cannot drift apart silently.

**`--output-schema` is not trusted.** The audit notes it is silently ignored when
tools or MCP servers are active. The plan is re-validated after every planner
run regardless, and the flag is omitted entirely if the schema file is missing.

### 2 — Validation profile

`.ai/validation.json` declares ordered, named checks; each is run independently
and recorded as its own evidence, so "tests passed but a schema is malformed" is
expressible. `{python}` expands to the running interpreter. `"required": false`
records a result without blocking. `"expect_test_count": true` applies the
empty-suite rule.

`test-results.json` gained `checks[]`, `acceptance_criteria`, and `diff_scope`,
and **kept every legacy top-level field** so `review-package.py` and older
readers keep working. A test asserts those fields still exist.

### 3 — Acceptance criteria are executed

`evaluate_acceptance_criteria()` runs each `verify` command and records
pass/fail/unverified per criterion. `verify: "judge"` counts as **unverified,
never passed** — routing it to a reviewer agent is Phase 2. A failing criterion
fails validation.

This closes the gap the audit calls its highest-value single change: the contract
the developer approved and the evidence they are shown are now the same
document.

### 4 — Diff-scope enforcement

`changed ⊆ files_to_modify ∪ files_to_create ∪ allowlist`, diffed against
`git merge-base` of `origin/main` then `main`. Undeclared changes fail
validation. `.ai/tasks/` is allowlisted so a task's own bookkeeping is never a
violation.

Note the consequence, confirmed in the live run: **plans must declare their test
files too.** That is correct — tests are part of the change — but it is a real
constraint on plan quality, and `AGENTS.md` now says so explicitly.

### 5 — Task creation and the `run` driver

- `new "<requirement>"` allocates the next id and creates the workspace at
  `NEW`; `advance` performs `NEW → ANALYZING → PLANNING`. Both states now have a
  real writer, so neither is decorative. `ANALYZING` is documented as the seam
  where Phase 2's clarification pass attaches.
- `run` advances to an approval gate, a terminal state, or a failure. Idempotent.
  It **re-checks the approval hash** rather than trusting the event, is bounded
  by `RUN_MAX_STEPS` and `RUN_MAX_FIX_ATTEMPTS`, and refuses to resume an
  interrupted `IMPLEMENTING` that has no routed failure.

### 6 — Worktree, PR, and the merge gate

- `worktree` creates `.worktrees/TASK-XXX` on the task branch and records it,
  finally using the schema field that was present and unused. The orchestrator
  still runs from the main checkout, where task artifacts live — relocating it
  into the worktree would be a much larger change and is not attempted.
- `pr` opens a **draft** PR with the review summary as its body. **Disabled
  unless `ORCHESTRATOR_ENABLE_PR=1`**, because `gh pr create` pushes the branch,
  and the developer's standing instruction is never to push. It was never
  invoked during this work.
- `complete` consults CI and **refuses while checks are failing or pending**.

**Judgement call on the CI gate.** The audit says completion should be blocked
until CI is green. Blocking on `unknown` too would make the workflow unusable
without `gh` or a remote, so `unknown` records `ci_status: unknown`, prints a
warning, and proceeds. Unverified is never *recorded* as success — but it does
not block. If you want the strict reading, that is a one-line change in
`complete_task`.

### 7 — A cross-platform bug caught only by the Windows run

`run_validation_check` substituted `{python}` into the command string and then
called `shlex.split()`. shlex uses **POSIX escaping**, so it consumed the
backslashes in a Windows interpreter path: `C:\Users\...\python.exe` became
`C:Users...python.exe` and the check could never run. Five tests failed on both
Windows interpreters while all 199 passed on WSL.

Fixed by tokenising *first* and substituting per token, so the real path never
passes through the splitter. Three regression tests cover it, one of them
skipping on platforms whose interpreter path has no backslashes.

The same portability gap existed in acceptance criteria — a `verify` command had
no way to name the interpreter — so `{python}` now expands there too.

**This is the case for verifying on the platform, not just the version.** WSL
alone would have shipped it.

### 8 — A diagnostic fix found by running it

The end-to-end run reported `exit code 5` for an empty suite. Python 3.12 exits
5 on a zero-test run while 3.9 exits 0 — the original TASK-004 defect — so the
exit-code branch was masking the precise cause. `evaluate_validation` now
diagnoses an empty suite *first*, whatever the exit code, and only reports an
exit code when tests actually ran and failed.

## Migration completeness

Every `plan.yaml` reference outside historical records was updated:

| File | Change |
|---|---|
| `AGENTS.md` | Rewritten output contract: JSON, schema, executed criteria, enforced file lists |
| `README.md` | Plan format, lifecycle, `run`/`fix`, validation section rewritten, worktree + PR + merge gate documented, CI matrix |
| `CLAUDE.md` | Verb list, plan-as-contract section, validation profile |
| `.claude/agents/lead-agent.md` | `plan.json` |
| `.github/workflows/ci.yml` | Validates both schemas and the validation profile |
| `.gitignore` | `.worktrees/` |

Also corrected in `README.md`: it still advertised the three review files Phase 0
deleted — an actively false claim about what the review package produces.

Remaining `plan.yaml` mentions are confined to `implementation.md` and
`phased-plan.md`, where they are accurate history of Phase 0, and to
`.ai/tasks/TASK-004/`, which is left untouched.

## New tests

| File | Tests | Covers |
|---|---|---|
| `test_plan_contract.py` | 34 | JSON parse, schema agreement, validator, codex argv, prompt |
| `test_validation_profile.py` | 34 | profile loading, per-check running, criteria, diff scope, cross-platform commands, integration |
| `test_run_driver.py` | 27 | task creation, bookkeeping transitions, driver gates and bounds |
| `test_worktree_pr.py` | 20 | real worktree creation, CI status parsing, merge gate, PR opt-in |

## Carry-forward

Still open, unchanged by this phase: **C5** (`dontAsk` flag name unverifiable
from this repo), **C3** (`classify_failure()` unreachable — Phase 2), **C7**
(replan prompt carries no failure context — Phase 3), **C9** (no cost
accounting — Phase 4).

New from this phase:

- **C10** — the orchestrator does not *run* in the worktree it creates. Full
  worktree execution needs path handling throughout. Phase 1 follow-on.
- **C11** — `complete` proceeds on `ci_status: unknown`. Decide whether the
  strict gate is wanted.
- **C12** — `validate_plan()` duplicates `plan.schema.json`. A `jsonschema`
  dependency would remove the duplication; a drift test guards it for now.
