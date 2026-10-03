"""Fixtures shared by this team's tests.

`repo_factory` builds a throwaway git repository from a list of commits, each a
mapping of path -> content (None deletes), and activates it as the run's
target with a default test command. `point` re-activates with a different
test or setup command. No test depends on the machine's own checkouts.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from swarmr_blame import output
from swarmr_blame import target as target_module
from swarmr_blame.target import Target, activate

Commit = tuple[str, Mapping[str, str | None]]
RepoFactory = Callable[..., Path]

DEFAULT_TEST = f"{shlex.quote(sys.executable)} check.py"


def point(repo: Path, test: str = DEFAULT_TEST, setup: str | None = None) -> Target:
    """Activate `repo` as the target with these commands, for this context."""
    params = {"repo": str(repo), "test": test}
    if setup:
        params["setup"] = setup
    return activate(params)


def _git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "color.ui=never", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(path),
            "GIT_AUTHOR_NAME": "Jan Visser",
            "GIT_AUTHOR_EMAIL": "jan@example.com",
            "GIT_COMMITTER_NAME": "Jan Visser",
            "GIT_COMMITTER_EMAIL": "jan@example.com",
            "GIT_AUTHOR_DATE": "2026-09-30T10:00:00+02:00",
            "GIT_COMMITTER_DATE": "2026-09-30T10:00:00+02:00",
        },
    )
    return done.stdout


@pytest.fixture
def repo_factory(tmp_path: Path) -> Iterator[RepoFactory]:
    """Build a repository from commits and point the team at it."""

    def build(
        commits: list[Commit], test: str = DEFAULT_TEST, setup: str | None = None
    ) -> Path:
        path = tmp_path / "repo"
        path.mkdir()
        _git(path, "init", "-q", "-b", "main")
        for message, files in commits:
            for rel, content in files.items():
                file = path / rel
                if content is None:
                    file.unlink()
                else:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(content)
            _git(path, "add", "-A")
            _git(path, "commit", "-q", "--allow-empty", "-m", message)
        point(path, test, setup)
        return path

    yield build
    target_module._current.set(None)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cached` keys on function name and arguments, and every test repository
    has the same shape of arguments, so a result would leak between tests."""
    monkeypatch.setattr(output, "_cache", {})
