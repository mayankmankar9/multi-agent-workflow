# Multi-Agent Development Workflow

This repository uses an AI-native product development workflow. A developer
never edits application code directly through the AI agents; instead, work
moves through a **Lead Agent**, a **Codex planning worker**, and a
**Claude Code implementation worker**, coordinated by a deterministic
orchestrator (`.ai/scripts/orchestrator.py`).

## Roles

### Lead Agent

The Lead Agent (`.claude/agents/lead-agent.md`) is the developer-facing
orchestration agent. It:

- Understands the developer's requested outcome.
- Creates and maintains task state under `.ai/tasks/TASK-XXX/`.
- Decides whether repository research or architectural planning is required.
- Invokes Codex to produce an implementation plan.
- Reviews the Codex plan for completeness and presents it to the developer
  for approval.
- Invokes Claude Code for implementation only after the plan is approved.
- Runs deterministic validation after implementation.
- Routes implementation failures back to Claude Code, routes material
  architecture or plan conflicts back to Codex, and asks the developer for
  clarification when requirements are ambiguous.
- Never bypasses repository security or deployment controls, and never
  deploys directly to production.
- Records material decisions and deviations in the task workspace, and
  reports task status clearly.

### Codex Planning Worker

Codex (`AGENTS.md`) is the planning worker. During planning, Codex:

- Inspects the repository but does not modify application source code.
- Does not create or delete repository files, and does not run
  state-changing commands.
- Produces a structured implementation contract (`plan.json`) covering the
  objective, requirements, files to modify/create, acceptance criteria,
  constraints, risks, open questions, and test strategy.

### Claude Code Implementation Worker

Claude Code (`CLAUDE.md`) is the implementation worker. Claude Code:

- May modify application code only after the developer has approved the
  implementation plan.
- Implements only the approved scope and preserves existing behavior outside
  that scope.
- Runs the tests required by the approved plan.
- Writes implementation notes to `.ai/tasks/TASK-XXX/implementation.md`.
- Does not modify task state or validation evidence; the orchestrator owns
  those artifacts.
- Reports material deviations from the approved plan instead of silently
  changing architectural decisions.

## Task Lifecycle and Approval Gates

Each task has a persistent workspace at `.ai/tasks/TASK-XXX/`, tracked in
`state.json` and validated against `.ai/schemas/task-state.schema.json`. The
orchestrator drives the task through the following states and gates:

1. `NEW` -> `ANALYZING` -> `PLANNING`: task intake
   (`orchestrator.py new "<requirement>"`, then `advance`).
2. `PLANNING` -> `AWAITING_APPROVAL`: Codex produces `plan.json`
   (`orchestrator.py TASK-XXX plan`).
3. **Developer approval gate**: the plan is presented to the developer, who
   must explicitly approve it (`orchestrator.py TASK-XXX approve`) before any
   code changes occur. A `PLAN_APPROVED` event records the plan version and
   the sha256 of the plan bytes.
4. `AWAITING_APPROVAL` -> `IMPLEMENTING`: only after an approval matching the
   current plan version *and* hash, Claude Code implements the approved plan on
   an isolated feature branch or worktree
   (`orchestrator.py TASK-XXX implement`).
5. `IMPLEMENTING` -> `VALIDATING` -> `VALIDATED`: deterministic validation
   runs (`orchestrator.py TASK-XXX validate`).
6. `VALIDATED` -> `PR_READY`: `orchestrator.py TASK-XXX review` runs three
   read-only reviewer subagents in parallel — correctness, security,
   performance — then builds the review package. It asserts nothing it did not
   observe. A `high`-severity finding sends the task to `FAILED` instead of
   `PR_READY`.
7. **Merge approval gate**: the developer approves the merge
   (`orchestrator.py TASK-XXX complete`), moving the task to `COMPLETED`.

### Before planning: clarification

`orchestrator.py TASK-XXX clarify` runs a read-only clarifier over the
requirement and writes `clarifications.json`. Ambiguity used to surface as
`open_questions` inside a plan the developer was already being asked to approve
— so they resolved it *while reviewing*, which is the expensive moment. Now the
questions are batched up front.

`advance` refuses to leave `ANALYZING` while any question is unanswered, and
`run` stops there. That is the third genuine human decision, alongside plan
approval and merge approval. Zero questions is a valid answer; the pass is
optional and never blocks if the agent is unavailable.

### Machine checks before the human gate

Two read-only agents sit in front of the developer's attention:

- **Plan critic** runs after planning and before `AWAITING_APPROVAL`. It checks
  coverage, internal consistency, whether each `verify` command actually settles
  its statement, and whether the file lists are complete. A `high`-severity
  objection sends the plan back to the planner (bounded rounds); anything less
  passes with the critique recorded. The developer only reviews plans that
  already passed a machine check.
- **Failure classifier** routes a failure to `CLAUDE_FIX`, `CODEX_REPLAN`, or
  `DEVELOPER_CLARIFICATION` with a confidence and a rationale, over the test
  output, failed criteria, scope violations, plan and notes. It replaces prefix
  matching on `failure_reason` that could never fire. Disable with
  `ORCHESTRATOR_DISABLE_CLASSIFIER=1`; if the agent is unavailable the
  deterministic fallback is used and the event records which was used.

Every checker is read-only — `Read, Grep, Glob`, no `Edit`, `Write` or `Bash`.
The one who produces must not be the one who approves.

### The task blackboard

Each task has an append-only blackboard at `.ai/tasks/TASK-XXX/context.jsonl`:
typed blocks recording what a worker learned — `repo_finding`, `decision`,
`constraint_discovered`, `deviation`, `failure_observation`, `open_question`,
`resolved_question`. `orchestrator.py TASK-XXX context` prints it.

The workers are one-shot subprocesses that never run concurrently, so there is
no live channel between them. What this builds instead is **sequential context
accumulation with guaranteed freshness**: each worker starts from everything the
system knows at that moment, and is required to write back what it learned.

### Hooks: enforcement, not requests

`.claude/settings.json` registers four hooks. Everything the project
instructions previously *asked* for is now a control. **Only exit code 2
blocks** — exit 1 blocks nothing, which is the most common hooks bug.

| Hook | Does |
|---|---|
| `SessionStart` | Injects the blackboard and `.ai/constitution.md`, so it does not depend on prompt text staying correct. |
| `PreToolUse` | **Denies** writes to orchestrator-owned artifacts, edits to `.ai/scripts/`, `.ai/schemas/`, `.ai/hooks/` and `.claude/settings.json`, `git commit`/`push`/`reset`/`rebase`, and shell redirection into any protected path. |
| `PostToolUse` | Byte-compiles an edited `.py` file so a syntax error surfaces inside the loop. Advisory: reports and exits 0. |
| `Stop` | Refuses to end a worker session that left `implementation.md` empty or appended no context block. |

The redirection check matters: writing through the shell bypasses the
`Edit`/`Write` permission surface entirely, so denying the tools without denying
redirection would be theatre.

`SessionStart` and `Stop` no-op unless `ORCHESTRATOR_TASK_ID` is set, so ordinary
sessions in this repository are unaffected. All four fail **open** on malformed
input — a hook that failed closed on a harness payload change would brick the
repo after any upgrade.

### Cross-task memory

- `.ai/constitution.md` — invariants every agent reads, so conventions are not
  rediscovered per task, at cost and possibly differently.
- `.ai/adr/` — decisions that outlive the task that made them.
  `orchestrator.py adr "<title>"` creates the next numbered record.

### Cost and isolation

`orchestrator.py TASK-XXX cost` totals the recorded `WORKER_USAGE` events:
duration always, plus tokens and dollars when the CLI reports them. A missing
cost is printed as `unknown`, **not** as zero — and a run that failed after
burning twenty minutes is still in the ledger.

The implementer is the only agent with write access, and it reads repository
content that flows into its own prompt, so a poisoned file could steer it.
`ORCHESTRATOR_IMPLEMENTER_WRAPPER` supplies a container boundary as a command
prefix:

```
ORCHESTRATOR_IMPLEMENTER_WRAPPER="docker run --rm -v $PWD:/w -w /w my-image"
```

Empty by default. The image, mounts and network policy are site decisions.

### Driving it

`orchestrator.py TASK-XXX run` advances the machine until it reaches an
approval gate, a terminal state, or an unrecoverable failure. It is idempotent
and safe to re-run from any state, so the developer types one command and is
interrupted only for the decisions at steps 3 and 7.

A routed failure leaves the task in `IMPLEMENTING` with a `CLAUDE_FIX_STARTED`
event; `orchestrator.py TASK-XXX fix` re-invokes the implementer with the
failure evidence attached. `run` does this automatically, up to a bounded
number of attempts, then hands back to the developer.

If validation fails, the orchestrator classifies the failure and routes it:

- Ordinary code defects or test failures return to Claude Code
  (`IMPLEMENTING`) for a fix.
- Material architecture or approved-plan conflicts return to Codex for
  replanning (`REPLANNING`, followed by a new `AWAITING_APPROVAL` cycle that
  requires developer re-approval).
- Requirement ambiguity is routed to the developer for clarification.

Approved architectural decisions are never changed silently.

## Validation

Validation is deterministic, and it fails rather than shrugging.

### The validation profile

`.ai/validation.json` declares an ordered list of named checks. Each runs
independently and is recorded as its own piece of evidence, so "tests passed but
a schema is malformed" is expressible instead of collapsing into one boolean.
`{python}` expands to the interpreter running the orchestrator. A check with
`"required": false` is advisory: its result is recorded but it does not block.

**An empty test suite is a hard failure.** A check marked
`"expect_test_count": true` must report a non-zero test count; `returncode == 0`
alone is not a pass, and output with no parseable count is a failure too.

### Acceptance criteria are executed

Every criterion in `plan.json` carries a `verify` command, and validation
**runs it**, recording pass/fail per criterion:

```json
{"id": "AC-1", "statement": "README.md exists.", "verify": "test -f README.md"}
```

`verify: "judge"` is recorded as *unverified* — never as passed. Routing those
to a reviewer agent is future work.

### Diff scope is enforced

The actual branch diff is compared against the plan's `files_to_modify` and
`files_to_create`:

```
changed_files ⊆ files_to_modify ∪ files_to_create ∪ allowlist
```

Anything changed but undeclared is a scope violation and fails validation. A
task's own artifacts under `.ai/tasks/` are allowlisted.

### Evidence

Results land in `test-results.json` (per-check results, per-criterion results,
scope report), `review-findings.json` (per-dimension reviewer output),
`validation.md`, and `events.jsonl`.

Nothing is asserted that was not observed. If a section has no evidence behind
it, it is omitted rather than hedged. And a reviewer that **did not run** is
reported as not having run — never as having found nothing:

```
- **correctness**: ran, reported no findings. Examined: widget.py.
- **security**: did not run (agent claude timed out after 900s)
```

Those are different claims, and collapsing them is what made the original
review package worse than no review package.

### CI

`.github/workflows/ci.yml` runs on every push and pull request targeting `main`
or `feature/**`, across a Python matrix of **3.9 and 3.12** with
`fail-fast: false` so one version's failure still reports the other:

- Compiles `.ai/scripts/orchestrator.py` and `.ai/scripts/review-package.py`.
- Validates `.ai/schemas/task-state.schema.json` and `.ai/schemas/plan.schema.json`.
- Runs the repository test suite.

## Branching and Pull Requests

`main` is a protected branch. The orchestrator refuses to run implementation
directly on `main` (or `master`) and requires an active feature branch or
worktree matching the task's recorded branch in `state.json`. All changes
must:

- Be implemented on an isolated feature branch (for example,
  `feature/TASK-XXX`) or Git worktree.
- Pass validation and review packaging before being marked `PR_READY`.
- Be delivered to `main` through a pull request, which triggers CI, rather
  than by pushing or merging directly to `main`.
- Receive explicit developer merge approval before the task is marked
  `COMPLETED`.

### Worktrees

`orchestrator.py TASK-XXX worktree` creates an isolated checkout at
`.worktrees/TASK-XXX` on the task branch and records it in `state.json`, so
several tasks can run without fighting over one working tree. The orchestrator
itself still runs from the main checkout, where task artifacts live.

### Pull requests and the merge gate

`orchestrator.py TASK-XXX pr` opens a **draft** pull request with the review
summary as the body. It is disabled unless `ORCHESTRATOR_ENABLE_PR=1`, because
`gh pr create` pushes the branch — an outward-facing action that should never
happen as a side effect of a local command.

`complete` consults CI for the task branch and **refuses** while checks are
failing or still running. If CI cannot be determined (no `gh`, no runs found),
it records `ci_status: unknown`, says so, and proceeds — unverified is never
recorded as success.

No agent deploys directly to production, and no agent bypasses human
approval or protected-branch controls.
