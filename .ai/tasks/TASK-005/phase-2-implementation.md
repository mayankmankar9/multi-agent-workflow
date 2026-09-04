# TASK-005 — Phase 2: Real intelligence

Implements Phase 2 of `phased-plan.md`. Branch `feature/TASK-005`. Not
committed — the developer reviews the diff.

Phase 0 made the gates honest. Phase 1 made them enforce the contract. Phase 2
puts **agents that cannot edit** in front of the human, so the developer's
attention goes to judgement rather than to catching what a machine could catch.

The governing principle throughout: *the one who produces must not be the one
who approves.* Every agent added here is read-only — `Read, Grep, Glob`, no
`Edit`, `Write` or `Bash` — and every verdict is structured output the
orchestrator re-validates rather than trusts.

## Verification status

**301 tests, all passing.** (Phase 0 ended at 84; Phase 1 at 204.)

| Version | Platform | Result |
|---|---|---|
| **3.9.25** | Windows native | 301 tests, exit 0 |
| **3.12.13** | Windows native | 301 tests, exit 0 |
| 3.14.4 | WSL Ubuntu | 301 tests, exit 0 (1 skip: Windows-only path test) |

### Honest limit on this phase's verification

**The agents were never actually invoked.** Neither `codex` nor `claude` is
available on this machine, so every agent interaction is exercised through
mocked subprocess boundaries: argv shape, output parsing, schema validation,
fallback behaviour, and every failure mode. What is *not* verified is whether a
real agent, given these prompts, returns useful findings.

That is a real gap and no amount of unit testing closes it. What the tests do
establish is that **no agent failure can corrupt the pipeline**: a missing
binary, a timeout, a non-zero exit, prose instead of JSON, a truncated
response, or schema-nonconforming output all resolve to "did not run", recorded
with a reason — never to a false clean bill of health.

The review-package rendering *was* verified end to end against a real
`review-findings.json`, which is where the dangerous output would appear.

## What changed

### Item 7 — Three parallel reviewer subagents

`run_reviewers()` runs `reviewer-correctness`, `reviewer-security` and
`reviewer-performance` concurrently via `ThreadPoolExecutor` (stdlib; the work
is subprocess-bound). Each returns findings against
`.ai/schemas/review-findings.schema.json`, re-validated by
`validate_findings()`.

A `high`-severity finding moves the task to `FAILED` rather than `PR_READY`.
Everything else is recorded and shown.

**This is where Phase 0's deleted sections are earned back.** The three fixed-
string files were removed because they asserted absences nobody verified. The
section now exists only because a real read-only agent produced it, and the
rendering keeps three claims strictly distinct:

```
- **correctness**: ran, reported no findings. Examined: widget.py.
- **performance**: 1 finding(s). Examined: widget.py.
  - `low` return value is constant — widget.py:2 (AC-1)
- **security**: did not run (agent claude timed out after 900s)
```

Compare TASK-004's committed summary, which said *"No secrets detected by this
workflow"* while nothing had scanned for secrets. "Did not run" and "found
nothing" are different claims, and collapsing them is precisely what made the
old package worse than no package.

The reviewer prompts forbid the failure mode explicitly: *"Never assert an
absence you did not verify."* A test asserts that sentence is present in all
three agent definitions, so the instruction cannot be quietly dropped.

### Item 8 — LLM failure classifier (closes C3)

`classify_failure_with_agent()` sends the failure reason, validation output,
failed acceptance criteria, scope violations, plan and implementation notes to
`failure-classifier`, which returns `{category, confidence, rationale}`.

The deterministic `classify_failure()` remains as the fallback, now documented
for what it is: **effectively a constant**. A test asserts all three strings the
orchestrator actually writes map to `CLAUDE_FIX`, which is the audit's 2.3
finding pinned down as a regression test rather than a paragraph.

Fallback is used, and recorded, when the agent is unavailable, returns invalid
output, or is disabled by `ORCHESTRATOR_DISABLE_CLASSIFIER=1`. The
`FAILURE_ROUTED` event now carries `classifier`, `confidence`, `rationale` and
`note`, so a routing decision can be audited instead of guessed at.

The classifier is told to **default to `CLAUDE_FIX`**: replanning discards
approved work and clarification blocks on a human, so both need positive
evidence.

### Item 9 — Clarification pass before planning

`clarify` writes `clarifications.json`; `advance` refuses to leave `ANALYZING`
while any `answer` is empty; `run` stops there as a human gate. Answered
questions are inlined into the planning prompt by
`clarification_context()`.

This adds the third genuine human decision the audit identifies in §5.1 —
alongside plan approval and merge approval. Ambiguity previously surfaced as
`open_questions` inside a plan the developer was already reviewing, so they
resolved it at the most expensive moment.

The clarifier is instructed that **zero questions is a good answer**, and told
not to ask what it can read for itself. A gate that fires spuriously teaches the
developer to skip it.

### Item 10 — Plan-quality critic before the human gate

`critique_plan()` runs `plan-critic` after planning and before
`AWAITING_APPROVAL`. A `high`-severity objection sends the plan back to the
planner with the critique appended to the prompt, bounded by
`CRITIC_MAX_ROUNDS = 2`. Lower severities pass with the critique recorded.

Its sharpest check is one only Phase 1 made possible: **does each `verify`
command actually settle its statement?** A criterion that passes trivially is
worse than none, because it manufactures confidence. The critic is told so
directly.

After the round limit the plan still goes to the developer, with
`PLAN_CRITIQUED` recorded — a critic that cannot be satisfied must not become a
gate nobody can pass.

### Cross-cutting: agents fail safe, never silent

`run_structured_agent()` treats every response as untrusted text.
`extract_json_object()` pulls the outermost braces out of fenced or prefaced
output, because the `--output-schema` family of flags is documented to be
silently ignored when tools are active. Then the result is parsed and validated.

Every failure path returns `(None, error)` and is surfaced as "did not run".
None of them can produce an empty finding list, which would read as "nothing
wrong".

## Events added

`REVIEW_COMPLETED`, `PLAN_CRITIQUED`, `CLARIFICATION_REQUESTED`,
`CLARIFICATION_SKIPPED`. Existing event names and their meanings are unchanged;
`FAILURE_ROUTED` gained fields but kept its name, so existing consumers that
switch on `event` keep working.

## New tests

| File | Tests | Covers |
|---|---|---|
| `test_agent_reviews.py` | 60 | read-only enforcement, JSON extraction, agent runner failure modes, findings validation, parallel reviewers, classifier + fallbacks, clarification gate, critic rounds |
| `test_review_package.py` | +17 (53 total) | findings rendering, did-not-run vs no-findings, criteria table, scope section, no fabricated claims or hedges with the new sections present |

The tests that matter most are the negative ones: a failed dimension is never
rendered as clean, a hedge never appears, and the fabricated-claims list from
Phase 0 is re-checked with the new sections populated.

## Carry-forward

**Closed this phase:** C3 (classifier), C12 — resolved by decision rather than
by change: `validate_plan()` stays hand-rolled, since validating against the
schema file needs a `jsonschema` dependency, and a drift test asserts the
required-key lists match.

**Still open:** C4 (legacy approvals fail closed — a decision, not a bug), C5
(`dontAsk` unverifiable here), C7 (replan prompt carries no failure context —
Phase 3), C9 (cost accounting — Phase 4), C10 (orchestrator does not run in the
worktree), C11 (`complete` proceeds on unknown CI).

**New:**

- **C13** — no agent has been executed for real. The prompts are unvalidated
  against actual model behaviour. Running one task end to end with `codex` and
  `claude` installed is the only way to close this.
- **C14** — reviewer findings are not deduplicated across dimensions. Three
  reviewers reading the same diff may report the same defect three times.
- **C15** — `judge` acceptance criteria are still recorded as unverified. Phase
  2 built the reviewer machinery they would route to, but did not wire them up.
  That is a natural small follow-on.
