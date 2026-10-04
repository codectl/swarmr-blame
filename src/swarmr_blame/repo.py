"""The target repository: where it is, and how git is invoked against it.

One responsibility: resolving which checkout this run investigates and running
git against it without a shell. Every other module that needs git goes through
`git()`; none of them spells a shell command.

The target is never mutated. Nothing here checks out, resets or writes refs in
the user's checkout; investigators work in worktrees under a scratch directory
(see `sandbox.py`), and the only git commands issued against the target itself
are reads plus `worktree add`/`worktree remove`, which touch `.git/worktrees`
and nothing in the working tree.

Which checkout is decided per run by `target.py` from the caller's `repo`
parameter; `target()` here is that answer, re-read on every call so two runs
in one process can point at two repositories. A path that is not the root of a
repository is an error, not a reason to search upwards and investigate
whatever is found there.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from swarmr.core.team import TeamError

__all__ = ["GitError", "RepoError", "git", "resolve", "target"]

_GIT_TIMEOUT = 120


class RepoError(TeamError):
    """The target checkout cannot be used; the message names the remedy."""


class GitError(RuntimeError):
    """A git command failed; carries the command's own stderr."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(argv)} exited {returncode}: {self.stderr}")


def target() -> Path:
    """The checkout under investigation for the active run."""
    # Local import: `target.py` validates a checkout with `git()` from here.
    from swarmr_blame.target import current

    return current().repo


def git(
    args: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    timeout: int = _GIT_TIMEOUT,
) -> str:
    """Run git with typed arguments and return stdout.

    No shell: `args` is passed as an argv list. Colour, pager, locale and path
    quoting are pinned so output is parseable regardless of the operator's git
    config: with `core.quotepath` on, a non-ASCII path arrives C-quoted on the
    `---`/`+++` lines and never matches the path the model asked about.
    """
    argv = [
        "git",
        "-c",
        "color.ui=never",
        "-c",
        "core.pager=cat",
        "-c",
        "core.quotepath=false",
        *args,
    ]
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(
        argv,
        cwd=cwd or target(),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and done.returncode != 0:
        raise GitError(args, done.returncode, done.stderr)
    return done.stdout


def resolve(ref: str) -> str:
    """The full sha `ref` names in the target checkout.

    Every ref a tool receives goes through here before anything is checked out,
    so "HEAD~3" means three before the user's HEAD regardless of which scratch
    worktree runs the test. Rejects anything shaped like an option.
    """
    stripped = ref.strip()
    if not stripped or stripped.startswith("-") or any(c.isspace() for c in stripped):
        raise ValueError(f"{ref!r} is not a ref")
    return git(["rev-parse", "--verify", "--quiet", f"{stripped}^{{commit}}"]).strip()
