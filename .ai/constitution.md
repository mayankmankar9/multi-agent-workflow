# Project constitution

Invariants that hold for **every** task in this repository. Every agent reads
this before planning or implementing.

This exists because conventions discovered in one task are otherwise
rediscovered in the next — at cost, and possibly differently. If something here
is wrong, change it deliberately and record why in an ADR; do not work around it
in a single task.

## Evidence

- **Report only what you observed.** Never assert an absence you did not verify.
  "No secrets detected" is a claim about your own thoroughness, not an
  observation about the code.
- **Omit, do not hedge.** A section with nothing behind it is left out. "Not
  assessed" still reads as a finding.
- **"Did not run" and "found nothing" are different claims.** Never let one
  stand in for the other.
- **Never report success you have not verified.** If you could not run
  something, say so plainly.

## Validation

- An empty test suite is a hard failure. So is output with no parseable test
  count. `returncode == 0` alone is not a pass.
- Tests are written alongside a change, never deferred. A fix without a test
  that would have caught the defect is incomplete.
- Fix the cause. Never weaken or delete a test to make it pass.

## The plan is a contract

- The plan is JSON conforming to `.ai/schemas/plan.schema.json`.
- Every acceptance criterion carries a `verify` command, and the orchestrator
  executes it. A criterion nothing can check is a criterion nobody checks.
- `files_to_modify` + `files_to_create` are compared against the real diff.
  Declare every file the change will touch, tests included.

## Roles

- The one who produces must not be the one who approves. Checkers are read-only
  and cannot edit what they judge.
- Write files through `Edit`/`Write`, never through `Bash` heredocs or shell
  redirection: that routes mutations around the tool permission surface.

## Boundaries

- The orchestrator owns task **evidence**. A worker never writes `state.json`,
  `events.jsonl`, `test-results.json`, `validation.md` or `review-findings.json`
  — no plan can authorise writing your own verdict, and the `PreToolUse` hook
  denies it unconditionally.
- Workflow **infrastructure** — `.ai/scripts/`, `.ai/schemas/`, `.ai/hooks/`,
  `.claude/settings.json` — is writable by a worker only when the approved plan
  declares the file in `files_to_modify` or `files_to_create`. The hook checks
  that against the approved plan's bytes, so an undeclared file is denied
  rather than reported afterwards. This repository's own source *is* its
  workflow, so a blanket ban made every self-hosted task unimplementable; the
  developer's hash-bound approval is what grants the access. See ADR-0004.
- Never `git commit` or `git push` unless the developer explicitly asks.
- `main` is protected. Implementation happens on a feature branch or worktree.
- Standard library only unless a dependency is explicitly approved.

## Style

- Match the surrounding code: its naming, its comment density, its idioms.
- Comments explain *why*, not *what*. A comment that restates the line is noise.
- Prefer an existing helper over a new one. Look before you add.
