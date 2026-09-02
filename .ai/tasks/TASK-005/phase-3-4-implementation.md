# TASK-005 — Phases 3 and 4: Context bus, hardening and ops

Implements Phases 3 and 4 of `phased-plan.md`. Branch `feature/TASK-005`. Not
committed — the developer reviews the diff.

Phase 3 gives the workers a shared memory. Phase 4 converts the project's
written rules into controls the harness enforces, and starts recording what a
task costs.

## Verification status

**402 tests, all passing.** (Phase 0: 84. Phase 1: 204. Phase 2: 301.)

| Version | Platform | Result |
|---|---|---|
| **3.9.25** | Windows native | 402 tests, exit 0 |
| **3.12.13** | Windows native | 402 tests, exit 0 |
| 3.14.4 | WSL Ubuntu | 402 tests, exit 0 (1 skip: Windows-only path test) |

### What is and is not verified here

Unlike Phase 2, **the hooks are genuinely verified.** They are ordinary scripts,
so the tests execute them as scripts, feed real JSON payloads on stdin, and
assert exit codes — including that a denial is exit **2** and never exit 1.

Two things remain unverified, and no test here changes that:

- **The harness invoking the hooks.** `.claude/settings.json` registers them
  correctly by inspection, but whether this harness fires them at these
  lifecycle events cannot be observed from inside a script. The scripts'
  behaviour is proven; the wiring is asserted.
- **The container boundary.** No container runtime is available, so
  `implementer_argv()` is verified to build a wrapped command, not to run one.

## Phase 3 — Context bus

### Item 13 / C7 — the replan no longer replans blind

This was the audit's sharpest criticism of cross-agent context. `run_codex_replan`
inlined `requirement.md` and nothing else: the replanner was told a failure had
happened and given zero information about it, so it re-derived from exactly the
inputs that produced the failing plan.

`replan_context()` now carries the failed plan's full text, the validation
output, the failed acceptance criteria with their `verify` commands and output,
the diff-scope violations, the reviewer findings, the classifier's rationale, and
the implementation notes.

One instruction in it matters more than the rest:

> If the previous plan was structurally sound and only the implementation was
> wrong, say so in `risks` — a replan is the wrong remedy for a bug.

Because `route_failure` bumps `plan_version` *before* replanning, the failing
plan is captured before the pointer moves. A test asserts the v1 contents reach
the replanner even though state already says v2.

`failure_evidence()` is now one builder shared by the fix prompt, the replan
prompt and the classifier. Previously each assembled its own subset — and the
replan path assembled none.

### Item 11 — the blackboard

`.ai/tasks/TASK-XXX/context.jsonl`, append-only, typed blocks:
`repo_finding`, `decision`, `constraint_discovered`, `deviation`,
`failure_observation`, `open_question`, `resolved_question`. Schema at
`.ai/schemas/context-block.schema.json`; `orchestrator.py TASK-XXX context`
prints it.

**Deviation from the audit, deliberate:** the audit names the file
`context.json`. A JSON array cannot be appended to without rewriting it, which
is exactly the read-modify-write hazard the task lock exists to prevent. JSONL,
matching `events.jsonl` in the same directory, is genuinely append-only.

The orchestrator writes its own blocks at the transitions it owns — a `decision`
on approval, a `failure_observation` on validation failure — and injects the
board into every worker prompt via `shared_context()`.

Injected content is marked as **data, not instructions**, because it is
untrusted output from a previous agent. A test asserts that framing survives.

### Item 12 — hooks that enforce rather than request

- **`SessionStart`** injects the blackboard and the constitution. Doing it in a
  hook means it does not depend on prompt text staying correct.
- **`Stop`** refuses to end a worker session that left `implementation.md` empty
  or appended no context block — with **exit code 2**, the only code that
  blocks. A test asserts orchestrator-authored blocks do *not* satisfy the rule:
  the worker must write back itself rather than coast on bookkeeping.

Both no-op unless `ORCHESTRATOR_TASK_ID` is set, which `worker_env()` exports.
Ordinary sessions in this repository are unaffected — a hook that fired outside
the workflow would make the repo unusable.

### Item 14 — cross-task memory

`.ai/constitution.md` holds the invariants (evidence rules, validation rules,
the plan-as-contract rules, role boundaries). `.ai/adr/` holds decisions that
outlive a task, with two records written: the JSON plan format, and read-only
checkers. `orchestrator.py adr "<title>"` creates the next one.

The ADR template demands a Consequences section and says why: *a record with
only benefits is not a decision, it is an advertisement.* Both existing records
state their real costs — legacy plans became unparseable; reviewers cannot run
commands, so findings needing execution are out of their reach.

## Phase 4 — Hardening and ops

### Item 15 — the full hook set

`PreToolUse` hard-denies what `CLAUDE.md` previously only asked for:

| Attempt | Result |
|---|---|
| `Write .ai/tasks/T/state.json` | DENIED |
| `Edit .ai/scripts/orchestrator.py` | DENIED |
| `Bash git commit` / `git push` / `git reset` | DENIED |
| `Bash echo x > .ai/tasks/T/state.json` | DENIED |
| `Write widget.py` | allowed |
| `Write .ai/tasks/T/implementation.md` | allowed |
| `Bash git status` | allowed |

The redirection check closes the loophole that made audit 2.9 dangerous: writing
through the shell bypasses the `Edit`/`Write` permission surface entirely, so
denying the tools without denying redirection would have been theatre.

`PostToolUse` byte-compiles an edited `.py` file so a syntax error surfaces
inside the loop rather than at validation. It is **advisory** — reports on
stderr, exits 0. A test asserts it never returns 2: a lint disagreement must not
deny a call that already succeeded.

**A real bug the tests caught.** `normalise()` used `path.lstrip("./")`, which
strips any leading run of `.` and `/` characters — turning
`.ai/tasks/x/state.json` into `ai/tasks/x/state.json` so that *every* protection
pattern silently failed to match. Twelve tests went red. That is a permission
bypass, not a cosmetic slip, and it is the clearest argument in this whole task
for testing the guard rather than eyeballing it.

The hooks also fail **open** on their own input: empty stdin, malformed JSON, an
unknown payload shape or an unknown tool all allow. A hook that failed closed on
a harness payload change would brick the repository after any upgrade.

### Item 16 — sandbox boundary

`ORCHESTRATOR_IMPLEMENTER_WRAPPER` supplies a command prefix for the implementer
invocation, e.g. `docker run --rm -v $PWD:/w -w /w img`. Empty by default.

The implementer is the only agent with write access, and it reads repository
content that flows into its own prompt — so a poisoned file can steer the one
worker that can change things. The wrapper is deliberately not hardcoded: the
right image, mounts and network policy are site decisions.

Verified to build a wrapped argv preserving `--permission-mode dontAsk` and the
tool allowlist. **Not** verified to run: no container runtime here.

### Item 17 / C9 — cost accounting

Every worker invocation records a `WORKER_USAGE` event with `duration_s` and,
when the CLI reports them, `cost_usd`, `input_tokens`, `output_tokens`.
`orchestrator.py TASK-XXX cost` totals them.

Two decisions worth stating:

- **A failed run is recorded too.** A run that burned twenty minutes and then
  failed is exactly the one worth seeing in the ledger.
- **Missing cost is `null`, never `0`,** and the report prints `unknown` with
  "Unknown, not zero". Reporting an unmeasured cost as free is the same class of
  error as reporting an unrun reviewer as clean.

Full OTel emission is not implemented. The conventions are pre-stable, there is
no collector here to verify against, and emitting spans nothing can read would
be unverifiable work. The event stream carries the same figures in a form this
repo can actually use — see C16.

## Carry-forward

**Closed:** C7 (replan context), C9 (cost accounting).

**Still open:** C4, C5, C10, C11, C13, C14, C15 — see `phased-plan.md`.

**New:**

- **C16** — no OTel emission. `WORKER_USAGE` events carry duration/tokens/cost;
  exporting them as `gen_ai.*` spans needs a collector to verify against, and
  the conventions are pre-stable, so pin a version when you do it.
- **C17** — hook *registration* is unverified. The scripts are tested; whether
  the harness fires them at these events is asserted, not observed. Running one
  task with the CLIs installed settles this and C13 together.
- **C18** — `PreToolUse` denies by path pattern. A worker could still write a
  protected file through a Python script it invokes via an allowed `Bash` call.
  Closing that properly needs the container boundary (item 16), not more
  patterns.
