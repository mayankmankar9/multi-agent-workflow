# Checkers are read-only agents

- **Status:** accepted
- **Date:** 2026-09-01
- **Supersedes:** —

## Context

The review package used to be produced by the same process that did the work,
writing fixed strings about its own output: "No secrets detected by this
workflow", "Changes are limited to the approved task scope". Nothing had
checked any of it. That document fed the human approval gate, which made it
worse than no evidence, because it looked like evidence.

The implementer was also invoked as `lead-agent`, an agent declaring no write
tools — so it either could not edit files or wrote them through Bash heredocs,
routing every mutation around the tool permission surface.

## Decision

The maker and the checker are separate agents with separate tool sets.

- `implementer` declares `Read, Edit, Write, Grep, Glob, Bash`.
- Every checker — `reviewer-correctness`, `reviewer-security`,
  `reviewer-performance`, `plan-critic`, `failure-classifier`, `clarifier` —
  declares exactly `Read, Grep, Glob`. No `Edit`, no `Write`, no `Bash`.

Checker output is structured, re-validated against a schema by the
orchestrator, and a checker that fails to run is recorded as *not having run*.

## Consequences

- A checker cannot fix what it is judging, so it cannot quietly make its own
  objection go away.
- A failed checker degrades to "did not run (reason)", never to "no issues
  found". The two claims are kept structurally distinct.
- Reviewers cannot run commands, so they reason from the diff and the files.
  That is a real limit: a finding that needs execution to confirm is out of
  reach for them, and belongs in a validation check instead.
