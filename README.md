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
- Produces a structured implementation contract (`plan.yaml`) covering the
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

1. `NEW` -> `ANALYZING` -> `PLANNING`: task intake and preparation for
   planning.
2. `PLANNING` -> `AWAITING_APPROVAL`: Codex produces `plan.yaml`
   (`orchestrator.py TASK-XXX plan`).
3. **Developer approval gate**: the plan is presented to the developer, who
   must explicitly approve it (`orchestrator.py TASK-XXX approve`) before any
   code changes occur. A `PLAN_APPROVED` event is recorded.
4. `AWAITING_APPROVAL` -> `IMPLEMENTING`: only after `PLAN_APPROVED`, Claude
   Code implements the approved plan on an isolated feature branch or
   worktree (`orchestrator.py TASK-XXX implement`).
5. `IMPLEMENTING` -> `VALIDATING` -> `VALIDATED`: deterministic validation
   runs automatically (`orchestrator.py TASK-XXX validate`).
6. `VALIDATED` -> `PR_READY`: a review package is generated
   (`orchestrator.py TASK-XXX review`), producing security, quality, and
   performance review notes plus a review summary for the pull request.
7. **Merge approval gate**: the developer approves the merge
   (`orchestrator.py TASK-XXX complete`), moving the task to `COMPLETED`.

If validation fails, the orchestrator classifies the failure and routes it:

- Ordinary code defects or test failures return to Claude Code
  (`IMPLEMENTING`) for a fix.
- Material architecture or approved-plan conflicts return to Codex for
  replanning (`REPLANNING`, followed by a new `AWAITING_APPROVAL` cycle that
  requires developer re-approval).
- Requirement ambiguity is routed to the developer for clarification.

Approved architectural decisions are never changed silently.

## Validation

Validation is deterministic and reproducible:

- Local/orchestrator validation runs `python3 -m unittest -v` and records the
  results as task evidence (`test-results.json`, `validation.md`,
  `events.jsonl`).
- CI (`.github/workflows/ci.yml`) validates workflow assets and behavior on
  every push and pull request targeting `main` or `feature/**` branches:
  - Compiles `.ai/scripts/orchestrator.py` and `.ai/scripts/review-package.py`.
  - Validates `.ai/schemas/task-state.schema.json`.
  - Runs the repository test suite with `python -m unittest -v`.

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

No agent deploys directly to production, and no agent bypasses human
approval or protected-branch controls.
