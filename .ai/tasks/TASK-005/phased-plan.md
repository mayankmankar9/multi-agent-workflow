# Phased implementation plan

Phase 0 is complete (see `implementation.md`). This is the forward plan, plus
the carry-forward register: issues found during Phase 0 that were deliberately
**not** absorbed into its diff. Each carries the phase that should pick it up.

The ordering principle throughout: automate the checkable part so the human's
attention goes where judgement is genuinely required. Phase 0 made the existing
gates real; the later phases make them meaningful.

---

## Carry-forward register (found in Phase 0, deferred)

| # | Issue | Phase | Why deferred |
|---|---|---|---|
| ~~C1~~ | ~~Real YAML parse of the plan~~ | — | **Closed in Phase 1.** Plan moved to JSON; `load_plan` is a real `json.loads` parse, no dependency. Legacy `.yaml` plans are reported as unparseable rather than silently empty. |
| ~~C2~~ | ~~`run_validation` hardcodes `python3`~~ | — | **Closed in Phase 0.** Originally deferred, then pulled back in: on Windows `python3` is a Store stub that exits non-zero without running anything, so the validation gate could not execute at all on the developer's own machine. Deferring a fix that made item 4 untestable was the wrong call. Now `validation_command()` uses `sys.executable`. |
| ~~C3~~ | ~~`classify_failure()` unreachable (audit 2.3)~~ | — | **Closed in Phase 2.** LLM classifier over test output, failed criteria, scope violations, plan and notes. The deterministic version stays as a recorded fallback, with a test pinning that it is effectively a constant. |
| C4 | Legacy approvals without `plan_sha256` fail closed | 1 | Correct default, but an in-flight task approved under old code needs one manual re-approval. Document or add a one-shot migration. |
| C5 | `dontAsk` flag name unverified | 1 | Claim inherited from the audit; cannot be checked from this repo. Confirm against the CLI, then delete this row. |
| ~~C6~~ | ~~`.python-version` pins 3.12, CI pins 3.9~~ | — | **Closed.** CI now runs a `["3.9", "3.12"]` matrix, so the pinned local version is one of the tested versions. Both verified locally and in CI. |
| ~~C7~~ | ~~Replan prompt carries no failure context~~ | — | **Closed in Phase 3.** `replan_context()` carries the failed plan, validation output, failed criteria, scope violations, reviewer findings, classifier rationale and implementation notes. |
| ~~C8~~ | ~~`NEW`, `ANALYZING`, `worktree` decorative~~ | — | **Closed in Phase 1.** `new` creates at `NEW`; `advance` performs `NEW → ANALYZING → PLANNING`; `worktree` uses the schema field. All three now have a real writer. |
| ~~C9~~ | ~~No cost or token accounting~~ | — | **Closed in Phase 4.** `WORKER_USAGE` events record duration always, plus cost and tokens when the CLI reports them. Missing cost is null, never zero. |
| C10 | The orchestrator does not run *in* the worktree it creates | 1+ | `worktree` prepares and records the tree, but the orchestrator still executes from the main checkout, where task artifacts live. Full worktree execution needs path handling throughout — deliberately not half-built. |
| C11 | `complete` proceeds on `ci_status: unknown` | 1+ | Blocking on unknown too would make the workflow unusable without `gh` or a remote. Unverified is recorded as unverified, never as success, but it does not block. One-line change in `complete_task` if the strict reading is wanted. |
| ~~C12~~ | ~~`validate_plan()` duplicates `plan.schema.json`~~ | — | **Closed in Phase 2 by decision.** Stays hand-rolled; a `jsonschema` dependency is not worth it. The drift test asserting the required-key lists match is the guard. |
| C13 | No agent has been executed for real | 3 | Neither `codex` nor `claude` is installed here. Every agent boundary is mocked; the prompts are unvalidated against actual model behaviour. Running one task end to end is the only way to close this. |
| C14 | Reviewer findings are not deduplicated | 3 | Three reviewers reading one diff may report the same defect three times. |
| C15 | `judge` criteria are not routed to a reviewer | 3 | Phase 2 built the machinery; wiring `verify: "judge"` into it is a small follow-on. |

---

## Phase 1 — Real validation

Turns level 1 of the verification ladder from theatre into a gate. Phase 0 made
the gate honest about *whether tests ran*; Phase 1 makes it honest about
*whether the work is correct*.

1. **Clear the carry-forward**: C1, C2, C4, C5, C6, C8.
2. **`.ai/validation.yaml` profile** — tests, lint, type check, secret scan,
   coverage floor, each recorded as separate evidence with its own pass/fail
   rather than one collapsed boolean.
3. **Executable acceptance criteria** (audit §4.2) — each criterion carries a
   `verify:` command; `validate` runs them and writes a per-criterion table.
   This is the audit's highest-value single change: it converts the plan from a
   document into a contract, and it is the mechanism by which the human can
   safely stop reading everything.
4. **Diff-scope enforcement** (§4.3) —
   `changed_files ⊆ files_to_modify ∪ files_to_create ∪ allowlist`.
   Deterministic, cheap, and it catches quiet blast-radius creep.
5. **`run` driver** (§5.2) — advance until an approval gate, a terminal state, or
   an unrecoverable failure. Idempotent.
6. **Worktree per task**; `gh pr create --draft` at `PR_READY`; `complete`
   blocked until CI is green.

*Prerequisite note:* items 3 and 4 both need the plan parsed for real, so C1
lands first.

## Phase 2 — Real intelligence

7. Replace the removed review files with **three parallel read-only reviewer
   subagents** — correctness, security, performance — each emitting structured
   findings against a schema and mapped to acceptance criteria. Phase 0 deleted
   the fabricated versions; this is what earns those sections back.
8. **LLM failure classifier** with structured output over
   `{test output, diff, plan, implementation notes}` → `{category, confidence,
   rationale}`, replacing the dead string matching (C3).
9. **Clarification pass** before planning (§4.4) — batch ambiguity to the
   developer up front instead of surfacing it inside a plan they are already
   being asked to approve.
10. **Plan-quality critic** before the human gate (§4.5).

## Phase 3 — Context bus

11. `context.json` blackboard with typed blocks (§3).
12. `SessionStart` hook injecting it; `Stop` hook enforcing write-back.
13. Replan prompt carries plan-vN, test output, and implementation notes (C7).
14. Repo `constitution.md` + ADR ledger for cross-task memory.

## Phase 4 — Hardening and ops

15. Full hook set (§4.6). Note the footgun: **only exit code 2 blocks**; exit 1
    blocks nothing.
16. Container/sandbox boundary for the implementer.
17. OTel emission + per-task cost in `events.jsonl` (C9).
18. Consider migrating the driver to the Claude Agent SDK — in-process
    orchestration, native structured output, hooks, subagents, cost visibility.
    Also the prerequisite for genuinely concurrent agents rather than sequential
    handoff.

---

## Rule for issues found mid-phase

Anything discovered while implementing a phase that is not in that phase's scope
gets added to this register with a target phase — not absorbed into the current
diff. Phase 0 followed this rule: the stale-state clobber in `route_failure()`
was fixed because it was inside the function being changed and would have
defeated the fix; everything else found along the way was recorded above instead.

---

## Register additions from Phases 3 and 4

| # | Issue | Phase | Note |
|---|---|---|---|
| C16 | No OpenTelemetry emission | later | `WORKER_USAGE` events already carry duration, tokens and cost. Exporting them as `gen_ai.*` spans needs a collector to verify against, and the conventions are pre-stable — pin a version. |
| C17 | Hook *registration* is unverified | later | The four hook scripts are tested by execution, including exit-code-2 denial. Whether this harness fires them at these lifecycle events is asserted from `settings.json`, not observed. Closing this also closes C13. |
| C18 | `PreToolUse` denies by path pattern | later | A worker could still write a protected file from inside a Python script invoked through an allowed `Bash` call. Closing it properly needs the container boundary, not more patterns. |

## Status

| Phase | State | Tests |
|---|---|---|
| 0 — Correctness | complete, verified | 84 |
| 1 — Real validation | complete, verified | 204 |
| 2 — Real intelligence | complete, agents mocked | 301 |
| 3 — Context bus | complete, hooks executed | 402 |
| 4 — Hardening and ops | complete, less OTel and container | 402 |

The audit's roadmap is implemented. What remains is not more construction but
**one real end-to-end run** with `codex` and `claude` installed: it closes C13,
C17 and C5 together, and it is the only thing that can tell you whether the
prompts produce useful output rather than merely well-formed output.
