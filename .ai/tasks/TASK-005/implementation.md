# TASK-005 — Phase 0 correctness fixes

Implements the nine Phase 0 items from `docs/audit-2026-09-01.md` §5.3.
Branch `feature/TASK-005`. Not committed — the developer reviews the diff.

## Verification status

**84 tests, all passing.** Before this task the same command reported
`Ran 0 tests` and the orchestrator scored it as a pass.

```
Ran 84 tests in 2.832s
OK
```

Also run: `py_compile` on both scripts, `json.tool` on the schema (the CI
checks), and a TASK-004 regression check confirming its `state.json` still
validates against the amended schema.

### Interpreter coverage

The Windows host had no Python — only Microsoft Store stubs, no `PythonCore`
registry entry. Interpreters were obtained with `uv`. All three CI steps
(`py_compile`, `json.tool`, `unittest`) pass on every version below.

| Version | Platform | Result |
|---|---|---|
| **3.9.25** | Windows (native) | 84 tests, exit 0 |
| **3.12.13** | Windows (native) | 84 tests, exit 0 |
| 3.14.4 | WSL Ubuntu | 84 tests, exit 0 |

3.9 and 3.12 are the two versions in the CI matrix. The Windows-native runs
also cover platform behaviour the WSL run could not:
`os.replace`, the `O_CREAT | O_EXCL` lock, `tempfile` paths, and the temp-git-repo
tests in `test_review_package.py`.

Two files outside this task's original scope were changed at the developer's
explicit request:

- `.python-version` (`3.12`) — written by `uv python pin`.
- `.github/workflows/ci.yml` — the single-version job became a
  `["3.9", "3.12"]` matrix with `fail-fast: false`, so one version's failure
  still reports the other's result. Both matrix versions are the two verified
  natively above, and 3.12 matches the local pin.

## What changed

| # | Audit | Change |
|---|---|---|
| 1 | 2.1 | Approval bound to `(plan_version, sha256(plan bytes))`, re-verified at implement time |
| 2 | 2.2 | `state.json.plan_file` + `resolve_plan_file()`; every reader goes through it |
| 3 | 2.4 | New `fix` verb — legal exit from `IMPLEMENTING` |
| 4 | 2.5 | `evaluate_validation()` — an empty suite is a hard failure |
| 5 | 2.6 | `review-package.py` rewritten: real diff, no unverified claims |
| 6 | 2.7, 2.8 | Fence closed; `validation.md` written |
| 7 | 2.9 | Dedicated `implementer.md` with declared write tools |
| 8 | 2.10 | `dontAsk` + explicit `--allowedTools` |
| 9 | 2.11 | Timeouts, atomic writes, task lock, plan pre-flight, branch order |

### 1 — Approval binding (2.1)

`approve_plan()` now records `plan_file` and `plan_sha256` on `PLAN_APPROVED`.
The unqualified `has_event(task_id, "PLAN_APPROVED")` check is replaced by
`find_approval(task_id, plan_version)`, which only matches an approval for the
*current* version. `verify_approval()` re-hashes the plan at implement time,
which also closes the post-approval tampering window.

The "already approved" short-circuit is gone. Re-approval is idempotent only
for identical bytes at the same version; edited bytes or a new version is a new
decision for the developer.

**Deliberate hard edge:** an approval with no recorded `plan_sha256` (i.e. any
approval made before this change, such as TASK-004's) **fails closed** and asks
for re-approval, rather than being trusted. TASK-004 is `COMPLETED` so nothing
breaks in practice, but an in-flight task approved under the old code would need
one `approve` re-run. Flagging rather than silently grandfathering.

### 2 — Canonical plan pointer (2.2)

`resolve_plan_file()` returns `state["plan_file"]` when set and falls back to
`plan.yaml` otherwise, so task directories written before the key existed stay
readable with **no migration**. The schema gains `plan_file` as an optional
nullable string.

**Extra bug found and fixed here.** `route_failure()`'s `CODEX_REPLAN` branch
saved its own stale `state` dict *after* calling `run_codex_replan()`. Had the
replan written `plan_file` into state itself, that trailing `save_state` would
have clobbered it — the same write-then-overwrite shape as the bug being fixed.
`run_codex_replan()` now returns `Optional[Path]` and the caller is the single
state writer. Slightly more restructuring than 2.2 literally asks for; confined
to that function pair.

### 3 — Fix loop (2.4)

New `fix` verb, legal only from `IMPLEMENTING` with a `CLAUDE_FIX_STARTED`
event present. `implement` keeps its single meaning, so its approval gate stays
unconditional. The fix prompt carries the actual failure evidence —
`failure_reason` plus the tail of `test-results.json` — because sending the
worker back with no information about what broke just re-derives the same
implementation. It also instructs the worker not to weaken or delete tests to
make them pass.

`VALID_NEXT_STATES` already advertised `IMPLEMENTING → VALIDATING`; that is now
true rather than aspirational.

### 4 — Empty suite fails (2.5), and the command can actually run

`parse_test_count()` and `evaluate_validation()` are **pure functions**. An
empty run, an unparseable run, or a non-zero exit all fail. `test_count` is
recorded in `test-results.json` and on the `VALIDATION` event.

**Related fix, initially deferred then pulled back in.** The validation command
was hardcoded as `python3 -m unittest -v`. On Windows `python3` resolves to a
Microsoft Store stub that exits non-zero without running anything, so the gate
could not execute on the developer's own machine — and the literal string was
duplicated into three evidence fields independently of the command actually
run, so they could drift apart. `validation_command()` now uses
`sys.executable`, with `python3` as a fallback for embedded interpreters.

I had logged this as carry-forward C2 because it is not in the audit. That was
wrong: it made item 4 untestable end to end, so it belonged in Phase 0.

**Why the rule is not a test.** A test module asserting "the suite is
non-empty" makes the suite non-empty and can therefore never fail — it is
unfalsifiable. Keeping the rule as a pure function lets the suite feed it
synthetic `Ran 0 tests` output and assert the verdict.
`test_exact_task_004_evidence_now_fails` replays the committed TASK-004 evidence
through the new gate and asserts it fails.

### 5 — Review package (2.6)

`security-review.md`, `quality-review.md` and `performance-review.md` are **no
longer generated at all**. Nothing scanned for secrets, compared the diff to
the plan, or looked at performance, so there is no file. Per instruction, no
`not assessed` hedge — a hedge still reads as a finding.

The diff is now real: base resolved via `git merge-base` against `origin/main`
then `main`, diffed as `<base>...HEAD`, excluding `.ai/tasks/` so a task's
review is not dominated by its own bookkeeping. If no base ref resolves, the
diff section is omitted and a warning is printed rather than reporting an empty
diff as though it meant "no changes".

Sections now present only when there is something behind them; the evidence
list names only files that exist on disk at write time.

### 6 — Fence and `validation.md` (2.7, 2.8)

Fences are balanced (`test_code_fences_are_balanced` counts them). `validation.md`
is written from `test-results.json` — command, return code, test count, result,
timestamp — resolving the dangling reference in the README and the evidence list
without touching the README.

### 7 — Implementer agent (2.9)

New `.claude/agents/implementer.md` declaring `Read, Edit, Write, Grep, Glob,
Bash`. It explicitly tells the worker to write via `Edit`/`Write` rather than
Bash heredocs, since shell redirection bypasses the tool permission surface and
makes `PreToolUse` matchers on `Edit`/`Write` useless. `lead-agent.md` is
untouched and stays read-only.

`test_allowlist_matches_the_agent_declaration` asserts the CLI allowlist is a
subset of what the agent declares, so the two cannot drift apart.

### 8 — Permission posture (2.10)

`--permission-mode dontAsk` plus `--allowedTools`, both in module-level
constants (`CLAUDE_PERMISSION_MODE`, `CLAUDE_ALLOWED_TOOLS`).

**Unverified claim, carried from the audit.** That `dontAsk` is the current flag
name and that `auto` is honoured only from user-scope settings are assertions
about the Claude Code CLI that cannot be checked from this repository. If the
flag name is wrong, the constants are the single place to fix it. I did not
independently confirm them.

### 9 — Operational hardening (2.11)

- **Timeouts** on every `subprocess.run`; `TimeoutExpired` fails the task.
- **Atomic writes** — `save_state` writes a sibling temp file, `fsync`s, then
  `os.replace`. A simulated mid-write crash is tested to leave the original
  intact.
- **Task lock** — `O_CREAT | O_EXCL` per task directory, released in `finally`,
  stale-lock message naming the holder PID. Atomic on both POSIX and Windows,
  so no `fcntl`. `status` is deliberately outside the lock so it stays readable
  while another invocation holds it.
- **Plan pre-flight** — `check_plan_wellformed()` rejects markdown fences, prose
  preambles, empty output, and missing top-level keys. **This is not a YAML
  parse** (see below). Verified to accept the real committed TASK-004 plan.
- **Branch order** — status is checked before the branch, so a state error
  reports as a state error.
- Removed the stray `\` on line 1 that made the shebang a continuation line.

## Where I think the audit is wrong

1. **§2.6 — wrong cause for the empty diff.** The audit blames
   `app.py`/`test_app.py` not existing. The deeper fault is the missing base
   ref: `git diff` with no ref compares worktree to index, so the diff would
   have been empty even if those files existed and were committed. Fixing only
   the pathspec would not have fixed the bug.
2. **§2.2 — versioned naming was already inconsistent.** v1 is never written as
   `plan-v1.yaml`; it exists only as `plan.yaml`. The audit's framing implies
   clean `plan-vN` history from the start; the mismatch begins at v1.
3. **§2.6 — task artifacts pollute a real diff.** `.ai/tasks/**` is git-tracked,
   which the audit does not mention. A naive `base...HEAD` diff makes a task's
   review mostly its own bookkeeping. Hence the exclusion pathspec.
4. **§2.11 — "YAML parse check" is not achievable as specified.** The standard
   library has no YAML parser and Phase 0 forbids dependencies. What landed is a
   structural pre-flight, named and documented as such rather than overclaiming.
5. **Not in the audit** — the committed stray `\` before the shebang.
6. **Not in the audit** — the stale-state clobber in `route_failure()` described
   under item 2.

Everything else in the nine items reproduced exactly as described.

## Not done, deliberately

- **Audit 2.3** (`classify_failure()` unreachable) is real — the three
  `failure_reason` strings never match `architecture:` or `requirement:`, so
  `CLAUDE_FIX` is the only reachable branch. The audit routes it to Phase 2
  item 16 (LLM classifier), so it is left alone. Consequence worth knowing:
  **every** routed failure currently goes down the `CLAUDE_FIX` path.
- No `state.json` or `requirement.md` was fabricated for TASK-005. Writing state
  implying an orchestrator run that never happened would be exactly the kind of
  synthetic evidence item 5 removes.
- `orchestrator.py` was never run against this task, per instruction.

## Event vocabulary

Unchanged — **no new event names.** The hash rides on `PLAN_APPROVED` as added
fields; fix mode is `IMPLEMENTATION_STARTED {mode: "fix"}`; empty-suite failure
is `VALIDATION {passed: false}`; approval-verification refusal is
`IMPLEMENTATION_FAILED {stage: "approval_verification"}`. Existing consumers
that switch on `event` keep working.

## Test map

| File | Tests | Covers |
|---|---|---|
| `test_plan_resolution.py` | 4 | plan pointer, replan freshness |
| `test_approval_binding.py` | 8 | version scoping, tampering, legacy fail-closed |
| `test_fix_loop.py` | 8 | `fix` verb, preconditions, failure context |
| `test_validation_gate.py` | 19 | count parsing, empty-suite rule, interpreter, timeout |
| `test_review_package.py` | 16 | real diff in a real temp git repo, no fabricated claims |
| `test_agent_config.py` | 9 | agent frontmatter, invocation flags |
| `test_orchestrator_infra.py` | 20 | timeouts, atomicity, lock, pre-flight, ordering |

The five mandated cases:

| Requirement | Test |
|---|---|
| Approval does not carry across a plan version bump | `FindApprovalTests.test_v1_approval_does_not_satisfy_v2`, `ImplementGateTests.test_replan_without_reapproval_is_refused` |
| Tampered plan rejected at implement time | `ImplementGateTests.test_tampered_plan_is_rejected_at_implement_time` |
| Implementer reads the current plan after a replan | `ImplementerReadsCurrentPlanTests.test_implementer_receives_replanned_plan_not_stale_plan_yaml` |
| A task in `IMPLEMENTING` has a legal next action | `FixLoopTests.test_fix_advances_implementing_to_validating` |
| An empty test suite fails validation | `EvaluateValidationTests.test_empty_suite_with_exit_zero_fails`, `RunValidationTests.test_empty_suite_moves_task_to_failed` |

## Follow-on work

See `phased-plan.md` in this directory for the phased roadmap and the
carry-forward register of issues found here but deferred.
