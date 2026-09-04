# Architecture decision records

Decisions that outlive the task that made them. A task-local choice belongs in
that task's `context.jsonl`; a choice the *next* task must not silently reverse
belongs here.

Numbered, immutable once merged. To change a decision, add a new record that
supersedes the old one and say so in both — the history of a reversal is usually
more useful than the reversal.

`orchestrator.py adr "<title>"` creates the next numbered record from the
template.

| # | Title |
|---|---|
| [0001](0001-plan-is-json.md) | The plan artifact is JSON, not YAML |
| [0002](0002-checkers-are-read-only.md) | Checkers are read-only agents |
| [0003](0003-hooks-are-launched-through-a-resolver-and-bind-workers.md) | Hooks are launched through a resolver and bind workers |
| [0004](0004-plan-declared-files-authorize-a-worker-evidence-never-is.md) | Plan-declared files authorize a worker; evidence never is |
