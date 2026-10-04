"""target: the run's repo/test/setup, validated once and ambient to every tool."""

from __future__ import annotations

import contextvars
import threading
from pathlib import Path

import pytest
from swarmr.core.team import TeamError

from swarmr_blame import repo, sandbox
from swarmr_blame.target import activate, current
from swarmr_blame.tests.conftest import RepoFactory, point


def test_activate_validates_the_checkout_and_parses_the_commands(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    path = repo_factory([("a", {"f": "1\n"})])
    t = activate({"repo": str(path), "test": "python3 check.py", "setup": "make deps"})
    assert t.repo == path.resolve()
    assert t.test.argv == ("python3", "check.py")
    assert t.setup is not None and t.setup.argv == ("make", "deps")
    assert repo.target() == path.resolve()

    with pytest.raises(TeamError, match="not a directory"):
        activate({"repo": str(tmp_path / "nope"), "test": "x"})
    (tmp_path / "plain").mkdir()
    with pytest.raises(TeamError, match="not inside a git work tree"):
        activate({"repo": str(tmp_path / "plain"), "test": "x"})
    (path / "sub").mkdir()
    with pytest.raises(TeamError, match="not its root"):
        activate({"repo": str(path / "sub"), "test": "x"})
    with pytest.raises(TeamError, match="`test` is empty"):
        activate({"repo": str(path), "test": ""})
    with pytest.raises(TeamError, match="`repo` is empty"):
        activate({"repo": "  ", "test": "x"})


def test_tools_outside_a_run_fail_with_a_sentence() -> None:
    def in_fresh_context() -> None:
        with pytest.raises(TeamError, match="No target is active"):
            current()

    contextvars.Context().run(in_fresh_context)


def test_the_target_is_per_context_not_per_process(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    """Two runs in one MCP server must not see each other's repository."""
    a = repo_factory([("a", {"f": "1\n"})])
    b = tmp_path / "b"
    b.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(b)], check=True)
    seen: dict[str, Path] = {}

    def other() -> None:
        point(b, test="true")
        seen["other"] = repo.target()

    # A new context (as a worker thread would get via copy_context) is isolated.
    contextvars.copy_context().run(other)
    assert seen["other"] == b.resolve()
    assert repo.target() == a.resolve()


def test_leases_are_keyed_by_repository(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    a = repo_factory([("a", {"f": "1\n"})])
    wa = sandbox.lease("t")
    b = tmp_path / "b"
    b.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(b)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(b),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "root",
        ],
        check=True,
    )
    point(b, test="true")
    wb = sandbox.lease("t")
    assert wa is not wb
    assert wa.repo == a.resolve() and wb.repo == b.resolve()
    assert wa.path != wb.path
    sandbox._release_all()


def test_the_target_survives_a_thread_that_copies_context(
    repo_factory: RepoFactory,
) -> None:
    """LangGraph fans tool calls out to worker threads with copy_context."""
    a = repo_factory([("a", {"f": "1\n"})])
    seen: list[Path] = []
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(lambda: seen.append(repo.target()),))
    thread.start()
    thread.join()
    assert seen == [a.resolve()]


def test_cached_reads_are_keyed_by_repository(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    """Two runs in one server asking `git_log("HEAD")` must not share an answer."""
    from swarmr_blame.history import git_log

    a = repo_factory([("only in a", {"f": "1\n"})])
    b = tmp_path / "b"
    b.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(b)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(b),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "only in b",
        ],
        check=True,
    )
    from_a = git_log.invoke({"rev_range": "HEAD", "limit": 5})
    point(b, test="true")
    from_b = git_log.invoke({"rev_range": "HEAD", "limit": 5})
    point(a)
    assert "only in a" in from_a and "only in b" not in from_a
    assert "only in b" in from_b and "only in a" not in from_b
