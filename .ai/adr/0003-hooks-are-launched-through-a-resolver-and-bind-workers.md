# Hooks are launched through a resolver and bind workers

- **Status:** accepted
- **Date:** 2026-09-02
- **Supersedes:** —

## Context

The four lifecycle hooks were registered in `.claude/settings.json` as
`python .ai/hooks/<script>.py`. They had never once run.

On Windows `python` and `python3` are App Execution Alias stubs that print
"Python was not found" and exit 9009; on most Linux images `python` does not
exist at all. Either way the hook process failed — and because **only exit code
2 denies**, a hook that fails any other way denies nothing. So `PreToolUse`
permitted every write it was written to refuse, `SessionStart` injected no
blackboard, and `Stop` enforced no write-back. The tests passed throughout:
they invoked the scripts with `sys.executable` directly, which is a working
interpreter, and separately asserted that the path registered in settings.json
existed. Neither ever asked whether the registered *command* ran.

This is the failure mode the repository exists to prevent, in the control layer
itself: a guard that reads as enforcement while enforcing nothing.

Two facts had to be observed rather than assumed before it could be fixed. A
probe hook reported `uname=MINGW64_NT`, so Claude Code runs hook commands under
Git Bash on Windows, not `cmd.exe` — one POSIX launcher covers both platforms.
And inside that shell both interpreter names resolve to the stubs even when a
real interpreter is installed, so a name is not enough.

Fixing it exposed the second decision. Once `PreToolUse` genuinely fired, it
denied every edit to `.ai/scripts/`, `.ai/schemas/`, `.ai/hooks/` and
`.claude/settings.json` from *any* session — including the developer's own, and
including its own source, which made the guard unfixable without bypassing it.
That absoluteness had never been felt because the hook had never run.

## Decision

**Hooks are launched through `.ai/hooks/run`**, a bash launcher that executes
each candidate interpreter before trusting it. Candidates in order:
`$ORCHESTRATOR_PYTHON`, `python3`, `python`, `py`. Acceptance is `"$c" -c pass`
succeeding, which is what separates a real interpreter from a stub. If none
work it exits 2 — loudly, because an environment with no working interpreter
cannot run this workflow at all.

`worker_env()` exports `ORCHESTRATOR_PYTHON` as the interpreter already running
the orchestrator, in POSIX form: bash cannot exec a Windows backslash path, and
exporting it verbatim silently fell through to whatever `python3` was on PATH.

**`PreToolUse` denies only when `ORCHESTRATOR_TASK_ID` is set** — the signal
`worker_env()` exports and nothing else does, and the one `SessionStart` and
`stop_guard` already gate on. The threat is a *worker* forging evidence or
committing on the developer's behalf. A developer editing `orchestrator.py`
deliberately is how this repository changes.

`preflight` executes what it reports on, and resolves Git Bash explicitly:
plain `bash` on Windows is `System32\bash.exe`, the WSL launcher, which is a
different filesystem with different interpreters and drops the environment
handed to it. Probing that would have validated a shell the hooks never use.

## Consequences

- The hooks are controls rather than decoration, for the first time.
- `preflight` fails on a machine where the workflow cannot run, instead of the
  first worker invocation failing thirty minutes in with a misleading message.
- A worker still cannot touch orchestrator-owned paths; the developer can. The
  cost is real: the machine-enforced boundary is now conditional on an
  environment variable the orchestrator sets. A worker cannot clear it for
  itself — it is set in the environment handed to the child — but anything that
  launches a worker *without* `worker_env()` gets an unguarded session. That is
  a narrower guarantee than "always on", and it is the price of a guard that
  can be maintained.
- C18 stands and is now the sharper edge: the guard matches paths on Edit/Write,
  so a worker could still write a protected file from inside a script invoked
  through an allowed `Bash` call. Closing it properly needs the container
  boundary, not more patterns.
- One launcher is a new dependency on bash being present. It is, on both
  platforms, in every environment this workflow already requires — git ships it
  on Windows.
