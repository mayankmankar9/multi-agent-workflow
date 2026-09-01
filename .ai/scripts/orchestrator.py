\
#!/usr/bin/env python3

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VALID_NEXT_STATES = {
    "NEW": "ANALYZING",
    "ANALYZING": "PLANNING",
    "PLANNING": "AWAITING_APPROVAL",
    "AWAITING_APPROVAL": "IMPLEMENTING",
    "IMPLEMENTING": "VALIDATING",
    "VALIDATING": "VALIDATED",
    "VALIDATED": "PR_READY",
    "PR_READY": "COMPLETED",
}

PROTECTED_BRANCHES = {"main", "master"}


def now() -> str:
    return datetime.now().astimezone().isoformat()


def task_dir(task_id: str) -> Path:
    return Path(".ai") / "tasks" / task_id


def load_state(task_id: str) -> tuple[Path, dict]:
    path = task_dir(task_id) / "state.json"

    if not path.is_file():
        raise FileNotFoundError(f"Task state not found: {path}")

    with path.open() as f:
        return path, json.load(f)


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now()

    with path.open("w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def record_event(task_id: str, event: str, **data) -> None:
    directory = task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    events_file = directory / "events.jsonl"

    payload = {
        "event": event,
        "task_id": task_id,
        "timestamp": now(),
        **data,
    }

    with events_file.open("a") as f:
        json.dump(payload, f, sort_keys=True)
        f.write("\n")


def has_event(task_id: str, event: str) -> bool:
    events_file = task_dir(task_id) / "events.jsonl"

    if not events_file.is_file():
        return False

    with events_file.open() as f:
        return any(
            json.loads(line).get("event") == event
            for line in f
            if line.strip()
        )


def current_branch() -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def verify_implementation_branch(task_id: str, state: dict) -> None:
    branch = current_branch()

    if branch in PROTECTED_BRANCHES:
        raise RuntimeError(
            f"Refusing implementation on protected branch: {branch}"
        )

    expected = state.get("branch")

    if expected and branch != expected:
        raise RuntimeError(
            f"Branch mismatch: state says {expected}, "
            f"repository is on {branch}"
        )

    if not branch:
        raise RuntimeError("Refusing implementation with detached HEAD.")


def run_codex_planning(task_id: str) -> None:
    repo_root = Path.cwd()
    directory = task_dir(task_id)
    requirement_file = directory / "requirement.md"
    plan_file = directory / "plan.yaml"

    if not requirement_file.is_file():
        raise FileNotFoundError(f"Requirement not found: {requirement_file}")

    prompt = f"""You are the planning worker for {task_id}.

Read the task requirement and inspect the repository.

Planning only:
- Do not modify application source code.
- Do not create or delete repository files.
- Do not run commands that change repository state.
- Produce only valid YAML in your final response.
- Do not use markdown code fences.

Required top-level fields:
objective:
requirements:
files_to_modify:
files_to_create:
acceptance_criteria:
constraints:
risks:
open_questions:
test_strategy:

Task requirement:

{requirement_file.read_text()}
"""

    subprocess.run(
        [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(repo_root),
            "--output-last-message",
            str(plan_file),
            prompt,
        ],
        cwd=repo_root,
        check=True,
    )

    print(f"Codex plan written to: {plan_file}")
    record_event(
        task_id,
        "PLAN_CREATED",
        plan_version=1,
        plan_file=str(plan_file),
        worker="codex",
    )


def run_plan(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "PLANNING":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "planning requires PLANNING."
        )
        return 1

    run_codex_planning(task_id)

    state["status"] = "AWAITING_APPROVAL"
    save_state(state_path, state)

    print(f"Task {task_id} moved to AWAITING_APPROVAL.")
    return 0


def approve_plan(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "AWAITING_APPROVAL":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "approval requires AWAITING_APPROVAL."
        )
        return 1

    if not (task_dir(task_id) / "plan.yaml").is_file():
        print("ERROR: plan.yaml is missing.")
        return 1

    if has_event(task_id, "PLAN_APPROVED"):
        print(f"Task {task_id} is already approved.")
        return 0

    record_event(
        task_id,
        "PLAN_APPROVED",
        plan_version=state.get("plan_version", 1),
        approved_by="developer",
    )
    print(f"Plan approved for {task_id}.")
    return 0


def run_claude_implementation(task_id: str) -> None:
    directory = task_dir(task_id)
    requirement_file = directory / "requirement.md"
    plan_file = directory / "plan.yaml"

    prompt = f"""Implement {task_id} using the approved plan.

Read:
- {requirement_file}
- {plan_file}

Implementation rules:
- Implement only the approved scope.
- Preserve existing behavior outside that scope.
- Do not modify .ai/tasks/{task_id}/state.json.
- Do not create or modify test-results.json.
- Do not change the orchestrator.
- Do not commit or push.
- Run the repository tests required by the approved plan.
- Write implementation notes to .ai/tasks/{task_id}/implementation.md.
- Do not write validation state; the orchestrator owns task state and validation evidence.

The developer has explicitly approved the plan.
"""

    subprocess.run(
        [
            "claude",
            "--print",
            "--agent",
            "lead-agent",
            "--permission-mode",
            "auto",
            prompt,
        ],
        cwd=Path.cwd(),
        check=True,
    )


def run_implementation(task_id: str) -> int:
    state_path, state = load_state(task_id)
    verify_implementation_branch(task_id, state)

    if state["status"] != "AWAITING_APPROVAL":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "implementation requires AWAITING_APPROVAL."
        )
        return 1

    if not has_event(task_id, "PLAN_APPROVED"):
        print(
            f"ERROR: TASK {task_id} has no PLAN_APPROVED event. "
            "Run the explicit approval action first."
        )
        return 1

    directory = task_dir(task_id)
    if not (directory / "plan.yaml").is_file() or not (
        directory / "requirement.md"
    ).is_file():
        print("ERROR: requirement.md or plan.yaml is missing.")
        return 1

    state["status"] = "IMPLEMENTING"
    save_state(state_path, state)

    record_event(
        task_id,
        "IMPLEMENTATION_STARTED",
        worker="claude",
        branch=state.get("branch"),
    )

    print(f"Task {task_id} moved to IMPLEMENTING.")

    try:
        run_claude_implementation(task_id)
    except subprocess.CalledProcessError as exc:
        state["status"] = "FAILED"
        state["failure_reason"] = (
            f"Claude implementation exited with code {exc.returncode}"
        )
        save_state(state_path, state)
        record_event(
            task_id,
            "IMPLEMENTATION_FAILED",
            worker="claude",
            returncode=exc.returncode,
            failure_reason=state["failure_reason"],
        )
        return exc.returncode

    state["status"] = "VALIDATING"
    save_state(state_path, state)
    record_event(task_id, "VALIDATION_STARTED", command="python3 -m unittest -v")

    print(f"Task {task_id} moved to VALIDATING.")
    return 0


def run_validation(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "VALIDATING":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "validation requires VALIDATING."
        )
        return 1

    directory = task_dir(task_id)
    results_file = directory / "test-results.json"

    print(f"Running validation for {task_id}...")

    result = subprocess.run(
        ["python3", "-m", "unittest", "-v"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
    )

    evidence = {
        "task_id": task_id,
        "command": "python3 -m unittest -v",
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "timestamp": now(),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }

    with results_file.open("w") as f:
        json.dump(evidence, f, indent=2)
        f.write("\n")

    record_event(
        task_id,
        "VALIDATION",
        passed=result.returncode == 0,
        returncode=result.returncode,
        command="python3 -m unittest -v",
        evidence_file=str(results_file),
    )

    output = result.stdout + result.stderr
    if output:
        print(output, end="")

    if result.returncode == 0:
        state["status"] = "VALIDATED"
        state["failure_reason"] = None
        save_state(state_path, state)
        print(f"Task {task_id} moved to VALIDATED.")
        return 0

    state["status"] = "FAILED"
    state["failure_reason"] = (
        f"Validation command failed with exit code {result.returncode}"
    )
    save_state(state_path, state)
    print(f"Task {task_id} moved to FAILED.")
    return result.returncode


def run_review(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "VALIDATED":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "review requires VALIDATED."
        )
        return 1

    subprocess.run(
        ["python3", ".ai/scripts/review-package.py", task_id],
        cwd=Path.cwd(),
        check=True,
    )

    state["status"] = "PR_READY"
    save_state(state_path, state)

    record_event(
        task_id,
        "PR_READY",
        branch=state.get("branch"),
        plan_version=state.get("plan_version", 1),
    )

    print(f"Task {task_id} moved to PR_READY.")
    return 0


def run_codex_replan(task_id: str) -> int:
    repo_root = Path.cwd()
    directory = task_dir(task_id)
    requirement_file = directory / "requirement.md"

    if not requirement_file.is_file():
        print(f"ERROR: Requirement not found: {requirement_file}")
        return 1

    state_path, state = load_state(task_id)
    plan_version = state.get("plan_version", 1)
    plan_file = directory / f"plan-v{plan_version}.yaml"

    prompt = f"""You are the planning worker for {task_id}.

The previous approved plan encountered an architecture conflict.

Re-analyze the repository and produce a revised implementation contract.

Planning only:
- Do not modify application source code.
- Do not create or delete repository files.
- Do not run state-changing commands.
- Produce only valid YAML.
- Do not use markdown code fences.

Required top-level fields:
objective:
requirements:
files_to_modify:
files_to_create:
acceptance_criteria:
constraints:
risks:
open_questions:
test_strategy:

Task requirement:

{requirement_file.read_text()}
"""

    subprocess.run(
        [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(repo_root),
            "--output-last-message",
            str(plan_file),
            prompt,
        ],
        cwd=repo_root,
        check=True,
    )

    record_event(
        task_id,
        "PLAN_CREATED",
        worker="codex",
        plan_version=plan_version,
        plan_file=str(plan_file),
        replanned=True,
    )

    print(f"Codex replanned task {task_id}: {plan_file}")
    return 0


def classify_failure(failure_reason: str) -> str:
    reason = failure_reason.lower()

    if reason.startswith("architecture:"):
        return "CODEX_REPLAN"

    if reason.startswith("requirement:"):
        return "DEVELOPER_CLARIFICATION"

    return "CLAUDE_FIX"


def route_failure(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "FAILED":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "failure routing requires FAILED."
        )
        return 1

    reason = state.get("failure_reason") or "unspecified failure"
    route = classify_failure(reason)

    print(f"Task: {task_id}")
    print(f"Failure: {reason}")
    print(f"Route: {route}")

    record_event(
        task_id,
        "FAILURE_ROUTED",
        route=route,
        failure_reason=reason,
    )

    if route == "CLAUDE_FIX":
        state["status"] = "IMPLEMENTING"
        save_state(state_path, state)
        record_event(task_id, "CLAUDE_FIX_STARTED", worker="claude")
        return 0

    if route == "CODEX_REPLAN":
        state["status"] = "REPLANNING"
        state["plan_version"] = state.get("plan_version", 1) + 1
        save_state(state_path, state)

        if run_codex_replan(task_id) != 0:
            state["status"] = "FAILED"
            state["failure_reason"] = "Codex replanning failed"
            save_state(state_path, state)
            return 1

        state["status"] = "AWAITING_APPROVAL"
        save_state(state_path, state)

        record_event(
            task_id,
            "REPLAN_READY_FOR_APPROVAL",
            plan_version=state["plan_version"],
        )

        print(f"Task {task_id} moved to AWAITING_APPROVAL.")
        return 0

    record_event(
        task_id,
        "DEVELOPER_CLARIFICATION_REQUIRED",
        failure_reason=reason,
    )
    return 0


def complete_task(task_id: str) -> int:
    state_path, state = load_state(task_id)

    if state["status"] != "PR_READY":
        print(
            f"ERROR: TASK {task_id} is in state {state['status']}; "
            "completion requires PR_READY."
        )
        return 1

    record_event(
        task_id,
        "MERGE_APPROVED",
        approved_by="developer",
    )

    state["status"] = "COMPLETED"
    save_state(state_path, state)

    record_event(
        task_id,
        "COMPLETED",
        branch=state.get("branch"),
    )

    print(f"Task {task_id} moved to COMPLETED.")
    return 0


def show_status(task_id: str) -> int:
    _, state = load_state(task_id)
    current = state["status"]
    next_state = VALID_NEXT_STATES.get(current)

    print(f"Task: {task_id}")
    print(f"Current state: {current}")

    if next_state:
        print(f"Next expected state: {next_state}")
    else:
        print("No automatic next state defined.")

    return 0


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: orchestrator.py TASK-XXX "
            "<status|plan|approve|implement|validate|review|"
            "route-failure|complete>"
        )
        return 2

    task_id = sys.argv[1]
    action = sys.argv[2]

    try:
        if action == "status":
            return show_status(task_id)

        if action == "plan":
            return run_plan(task_id)

        if action == "approve":
            return approve_plan(task_id)

        if action == "implement":
            return run_implementation(task_id)

        if action == "validate":
            return run_validation(task_id)

        if action == "review":
            return run_review(task_id)

        if action == "route-failure":
            return route_failure(task_id)

        if action == "complete":
            return complete_task(task_id)

        print(f"Unknown action: {action}")
        return 2

    except (FileNotFoundError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
