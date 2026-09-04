# Plan-declared files authorize a worker; evidence never is

- **Status:** accepted
- **Date:** 2026-09-02
- **Supersedes:** —
- **Amends:** [0002](0002-checkers-are-read-only.md)

## Context

`PreToolUse` denied every write to `.ai/scripts/`, `.ai/schemas/`,
`.ai/hooks/` and `.claude/settings.json`, for any session, with no exception.

That was airtight and unusable. This repository's only source **is** its
workflow infrastructure, so the rule meant no worker could ever implement
anything here: the pipeline was fully built, fully tested, and structurally
incapable of doing work on its own repo. The condition had never been noticed
because the hook was registered as `python .ai/hooks/...` and had never run
(ADR-0003).

The first real end-to-end task made it concrete. TASK-006 — make text I/O
specify UTF-8 — necessarily modifies `orchestrator.py`, `review-package.py` and
two hooks. Approving it and running it would have produced a denial on the
implementer's first edit.

The implementer itself flagged the tension rather than assuming past it, in
`context.jsonl` block `ctx-006`:

> The constitution lists `.ai/scripts/` among the paths the implementer must
> never write, but plan v1 declares `.ai/scripts/orchestrator.py` and
> `review-package.py` in `files_to_modify` — the task is about those files. I
> treated the approved plan's explicit declaration as the authority [...]
> Flagging it because the two rules read as being in tension; the resolution
> should be deliberate rather than assumed.

That is the blackboard working as designed, and it is why this record exists
instead of a silent convention.

## Decision

Two tiers, because they answer different questions.

**Evidence is never writable by a worker.** `state.json`, `events.jsonl`,
`test-results.json`, `validation.md`, `review-findings.json`. Denied
unconditionally. No plan can authorise a worker to write its own verdict; a
plan that asked would be the clearest possible signal something had gone wrong.

**Infrastructure is writable when the approved plan declares it.**
`.ai/scripts/`, `.ai/schemas/`, `.ai/hooks/`, `.claude/settings.json` are
allowed only for paths in the plan's `files_to_modify` or `files_to_create`.

The hook re-reads the plan and **re-hashes it**, comparing against the
`plan_sha256` in the `PLAN_APPROVED` event. It does not trust `state.json`'s
claim about the plan. So a plan edited after approval authorises nothing, an
approval for v1 does not carry into v2, and a missing or unparseable plan
authorises nothing — it fails closed.

The guard applies only to worker sessions, keyed on `ORCHESTRATOR_TASK_ID`
(ADR-0003). The shell route stays closed either way: a declared file may be
edited through `Edit`/`Write`, never through redirection or `sed`/`tee`.

## Consequences

- The workflow can modify its own repository, which is the only way this repo
  can be developed by it at all.
- The developer's hash-bound approval becomes an enforcement input rather than
  only a bookkeeping record. `files_to_modify` now means something at the
  moment of the write, not just at validation time — which matters more than
  expected, because diff-scope validation compares `merge-base...HEAD` and
  therefore sees nothing at all while the worker's changes are uncommitted.
  The hook is currently the *only* scope enforcement that observes them.
- The cost: the boundary is now conditional on data a worker can read. A worker
  cannot forge the `PLAN_APPROVED` event (that file is evidence, denied
  unconditionally), so the authorization cannot be self-granted — but the
  guarantee is narrower than "infrastructure is never writable", and reviewers
  should read a plan's `files_to_modify` as a grant of access, not just a
  statement of intent.
- C18 stands and matters more: the guard matches paths on `Edit`/`Write`, so a
  worker could still write a protected file from inside a script invoked
  through an allowed `Bash` call. Closing that needs the container boundary.
