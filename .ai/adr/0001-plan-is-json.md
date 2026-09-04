# The plan artifact is JSON, not YAML

- **Status:** accepted
- **Date:** 2026-09-01
- **Supersedes:** —

## Context

The plan was emitted as YAML and never parsed: the planner's raw last message
was written straight to disk. A fenced or truncated response produced an
unusable plan that nothing noticed, and the acceptance criteria and file lists
inside it could not be read, let alone enforced.

The standard library has no YAML parser, and this repository deliberately has no
dependencies. So parsing YAML meant either adding PyYAML or hand-writing a
parser for a format whose producer we do not control.

## Decision

The plan is JSON (`plan.json`, `plan-vN.json` after a replan), conforming to
`.ai/schemas/plan.schema.json`, and is parsed with `json.loads`.

The schema is also passed to the planner via `codex exec --output-schema`, but
its output is re-validated regardless: that flag is documented to be silently
ignored when tools or MCP servers are active.

## Consequences

- A real parse, with no dependency.
- Acceptance criteria and file lists became enforceable, which is what Phase 1
  is built on.
- Plans written before this decision cannot be parsed. They are reported as
  unparseable with a reason, never as empty — and re-planning regenerates them.
- Validation of the parsed plan is hand-rolled rather than schema-driven, since
  schema validation would need `jsonschema`. A drift test asserts the schema's
  required keys and the orchestrator's list stay in agreement.
