"""What one run investigates: the checkout, the test command, the setup command.

One responsibility: turning the caller's three per-call parameters into a
validated `Target` and making it reachable from every tool during that run.

The parameters arrive in `RunContext.params` — named in the MCP call or as CLI
flags — never from the environment. Nothing here reads `os.environ`: a process
serving several callers cannot have one repository, and an investigation
reported against the wrong checkout is worse than one that refuses to start.

The target rides a `ContextVar`. Tools are module-level functions that take
refs, not repositories (the model must never name a path or a command), so
they need an ambient way to find the run's target, and a context variable is
the one ambient channel that is per task rather than per process. `build`
activates it in the thread that then streams the graph; LangGraph copies the
context into the worker threads it fans out to, so every tool call sees the
target of the run that invoked it.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from swarmr.core.team import Param, TeamError

from swarmr_blame.command import Command
from swarmr_blame.repo import GitError, RepoError, git

__all__ = ["PARAMS", "Target", "TargetError", "activate", "current"]

PARAMS = (
    Param(
        "repo",
        "Absolute path of the git checkout to investigate; its root, not a "
        "subdirectory. Never modified: every test runs in a scratch worktree.",
    ),
    Param(
        "test",
        "The command whose exit code says whether the repository is broken: 0 "
        "passes, anything else fails, exactly as `git bisect run` expects. Run "
        'from the repository root. Examples: "terraform test -filter=x.tftest.hcl", '
        '"uv run pytest tests/test_cart.py::test_total", "go test ./... -run TestX", '
        '"bash check.sh".',
    ),
    Param(
        "setup",
        "Optional command that prepares a fresh checkout before the test can run, "
        'for example "terraform init -backend=false", "npm ci", "uv sync". Run '
        "once per scratch worktree and again whenever a lockfile changes.",
        required=False,
    ),
)


class TargetError(TeamError):
    """The run has no usable target; the message names what to supply."""


@dataclass(frozen=True, slots=True)
class Target:
    repo: Path
    test: Command
    setup: Command | None


_current: ContextVar[Target | None] = ContextVar("swarmr_blame_target", default=None)


def _checkout(raw: str) -> Path:
    """`raw` as the root of a git work tree, or a `RepoError` naming the remedy."""
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise RepoError(f"repo={raw!r} is not a directory.")
    try:
        top = git(["rev-parse", "--show-toplevel"], cwd=path)
    except GitError as exc:
        raise RepoError(
            f"{path} is not inside a git work tree ({exc.stderr}). Pass the "
            "checkout to investigate as `repo`."
        ) from exc
    top_path = Path(top.strip()).resolve()
    if top_path != path:
        raise RepoError(
            f"{path} is inside the repository at {top_path} but is not its root. "
            f"Pass repo={top_path}."
        )
    return path


def activate(params: dict[str, str] | None) -> Target:
    """Validate the caller's params and make them this run's target.

    `Team.context` has already guaranteed the required names are present; this
    checks what they point at, which only this team can judge.
    """
    given = params or {}
    test = Command.parse(given.get("test", ""))
    if test is None:
        raise TargetError(
            "`test` is empty. It must be the command that runs the failing test, "
            'exit 0 for pass; for example "terraform test -filter=x.tftest.hcl".'
        )
    target = Target(
        repo=_checkout(given.get("repo", "")),
        test=test,
        setup=Command.parse(given.get("setup", "")),
    )
    _current.set(target)
    return target


def current() -> Target:
    """The active run's target, or an error when no run activated one."""
    target = _current.get()
    if target is None:
        raise TargetError(
            "No target is active: a tool was called outside a run. The team's "
            "`build` activates the target from the call's repo/test/setup."
        )
    return target
