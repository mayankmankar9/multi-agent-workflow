# Codex Project Instructions

## Purpose

Analyze the repository and produce implementation plans.

## Planning Rules

- Do not modify source code during the planning phase.
- Identify affected files and interfaces.
- Prefer existing repository patterns.
- State assumptions and open questions.
- Analyze dependencies and architectural impact.
- Identify risks.
- Define acceptance criteria.
- Define the test strategy.
- Produce a structured implementation contract.

## Output

The plan is a **JSON object** written to `.ai/tasks/TASK-XXX/plan.json`
(replans go to `plan-vN.json`). It must conform to
`.ai/schemas/plan.schema.json`, which is passed to `codex exec` via
`--output-schema`.

Respond with the JSON object and nothing else: no markdown code fences, no
prose before or after. The orchestrator parses the file and rejects the plan if
it does not parse or does not conform — a plan it cannot read is not a plan.

Required top-level keys:

- `objective` — one sentence
- `requirements` — list of strings
- `files_to_modify`, `files_to_create` — list of `{path, purpose}`
- `acceptance_criteria` — list of `{id, statement, verify}`
- `constraints`, `risks`, `open_questions`, `test_strategy` — lists of strings

## Acceptance criteria are executed

Every criterion carries a `verify` command, and the orchestrator **runs it** at
validation time, recording pass/fail per criterion. A criterion nothing can
check is a criterion nobody checks.

- Prefer deterministic checks: `test -f path`, `git diff --quiet -- path`, a
  specific test selector.
- Use the literal `"judge"` only where no command can express the criterion. It
  is recorded as unverified, so use it sparingly.
- `id` must match `AC-<number>` and be unique within the plan.

## File lists are enforced

`files_to_modify` and `files_to_create` are compared against the actual
implementation diff. Anything changed but undeclared is a scope violation and
fails validation. Declare every file the implementation will touch.

## Important

Codex is the planning worker.

Do not implement the requested feature during planning.
