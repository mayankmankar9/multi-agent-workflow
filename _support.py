"""Shared helpers for the workflow test suite.

Deliberately named with a leading underscore so ``python -m unittest``
discovery (pattern ``test*.py``) does not collect it as a test module.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCRIPTS = REPO_ROOT / ".ai" / "scripts"


def load_script(filename: str, module_name: str):
    """Import a script that is not normally importable.

    ``.ai`` is not a valid package name and ``review-package`` is hyphenated,
    so neither target can be reached with a plain import statement.
    """
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(module_name, path)

    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def worker_name(argv) -> str:
    r"""Which worker CLI a mocked subprocess call is invoking.

    ``run_worker`` resolves ``argv[0]`` to a full path before exec, because npm
    ships both workers as ``.CMD`` shims on Windows and ``CreateProcess`` will
    not find those by bare name. Test doubles that dispatch on the worker must
    therefore compare the stem rather than the whole string: ``claude``,
    ``/usr/bin/claude`` and ``C:\npm\claude.CMD`` are all the same worker.
    """
    return Path(argv[0]).stem.lower()


def load_orchestrator():
    return load_script("orchestrator.py", "workflow_orchestrator")


def load_review_package():
    return load_script("review-package.py", "workflow_review_package")


VALID_PLAN = {
    "objective": "Add a widget.",
    "requirements": ["The widget exists."],
    "files_to_modify": [],
    "files_to_create": [{"path": "widget.py", "purpose": "the widget"}],
    "acceptance_criteria": [
        {
            "id": "AC-1",
            "statement": "widget.py exists.",
            "verify": "{python} -c \"import os,sys; sys.exit(0)\"",
        }
    ],
    "constraints": ["Standard library only."],
    "risks": ["None material."],
    "open_questions": [],
    "test_strategy": ["Run the unit suite."],
}


def plan_json(**overrides) -> str:
    """A schema-conforming plan, with fields overridable per test."""
    data = json.loads(json.dumps(VALID_PLAN))
    data.update(overrides)
    return json.dumps(data, indent=2) + "\n"


class TaskDirCase(unittest.TestCase):
    """Base case providing an isolated repo-shaped temp directory.

    Every test runs against a throwaway tree with its own ``.ai/tasks/``, so
    no test can read or write the real task directories.
    """

    task_id = "TASK-999"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.task_path = self.tmp / ".ai" / "tasks" / self.task_id
        self.task_path.mkdir(parents=True)

        self._prev_cwd = Path.cwd()
        import os

        os.chdir(self.tmp)
        self.addCleanup(os.chdir, str(self._prev_cwd))

    def write_state(self, **overrides):
        state = {
            "task_id": self.task_id,
            "status": "AWAITING_APPROVAL",
            "created_at": "2026-09-01T10:00:00+05:30",
            "updated_at": "2026-09-01T10:00:00+05:30",
            "plan_version": 1,
            "branch": "feature/%s" % self.task_id,
            "worktree": None,
            "failure_reason": None,
        }
        state.update(overrides)

        path = self.task_path / "state.json"
        path.write_text(json.dumps(state, indent=2) + "\n")
        return path, state

    def read_state(self):
        return json.loads((self.task_path / "state.json").read_text())

    def init_git(self):
        """Make the temp tree a repository.

        The baseline manifest is enumerated with ``git ls-files``, so a tree
        that is not a repo cannot produce a delta at all -- and the orchestrator
        already requires git for branch verification and the diff base.
        """
        if (self.tmp / ".git").exists():
            return

        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "t@example.com"),
            ("config", "user.name", "T"),
        ):
            subprocess.run(
                ["git"] + list(args),
                cwd=str(self.tmp),
                capture_output=True,
                check=True,
            )

    def write_baseline(self, entries=None, **overrides):
        """A verified task baseline, as ``capture_baseline`` would leave it.

        Written through the orchestrator's own digest and event so tests
        exercise the same verification path validation does, rather than a
        second hand-rolled notion of a valid baseline.

        With no explicit ``entries`` the real working tree is hashed, so
        whatever the fixture has already written counts as pre-existing and
        whatever it writes afterwards counts as task-produced. Call it at the
        point in the fixture where implementation would begin.
        """
        orch = load_orchestrator()
        self.init_git()

        if entries is None:
            entries, _ = orch.tree_manifest()

        payload = {
            "task_id": self.task_id,
            "captured_at": "2026-09-01T10:00:00+05:30",
            "plan_version_at_capture": 1,
            "git": {"head": None, "branch": None, "merge_base": None},
            "manifest_algo": "sha256",
            "truncated": False,
            "entries": entries,
        }
        payload.update(overrides)
        payload["entry_count"] = len(payload["entries"])
        payload["baseline_sha256"] = orch.canonical_baseline_digest(payload)

        path = self.task_path / "baseline.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")

        with (self.task_path / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "event": "BASELINE_CAPTURED",
                        "baseline_sha256": payload["baseline_sha256"],
                    }
                )
                + "\n"
            )

        return path, payload

    def baseline_of(self, *paths):
        """Manifest entries for files that exist in the temp tree right now."""
        orch = load_orchestrator()

        return {
            orch.normalise_repo_path(path): {
                "sha256": orch.digest_of_file(Path(path)),
                "size": Path(path).stat().st_size,
            }
            for path in paths
        }

    def write_plan(self, name="plan.json", body=None):
        path = self.task_path / name

        if body is None:
            body = plan_json()

        path.write_text(body)
        return path

    def write_requirement(self, body="Do the thing.\n"):
        path = self.task_path / "requirement.md"
        path.write_text(body)
        return path

    def events(self):
        path = self.task_path / "events.jsonl"

        if not path.is_file():
            return []

        return [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]


@contextlib.contextmanager
def quiet():
    """Swallow orchestrator stdout so test output stays readable."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
