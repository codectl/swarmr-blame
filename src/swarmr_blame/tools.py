"""The single-run oracle tool, and the tool set each role is given.

One responsibility: the one tool that runs the operator's test once at one
ref, plus the per-role tool sets assembled from every tool in the package. The
tools carrying their own knowledge live next door: `walkback`, `bisection` and
`comparison` compose the oracle into a search, `history` reads commits, `hunks`
projects a diff, `lockfiles` projects a dependency change.

Design rules:
  * No shell, and no command argument. The test command is the operator's
    (the `test` parameter); the model only ever chooses refs and counts. Nothing the
    model emits reaches a shell.
  * Nothing runs in the user's checkout. Every oracle call happens in a leased
    worktree under scratch; the user's HEAD, index and working tree are never
    touched.
  * Oracle tools are never cached. A rerun is the point of a rerun.
"""

from __future__ import annotations

from langchain_core.tools import tool

from swarmr_blame import oracle
from swarmr_blame.bisection import bisect
from swarmr_blame.comparison import compare_refs
from swarmr_blame.history import git_log, git_show
from swarmr_blame.hunks import diff_hunks
from swarmr_blame.lockfiles import lockfile_diff
from swarmr_blame.output import emit, guard
from swarmr_blame.sandbox import lease
from swarmr_blame.walkback import walk_back

__all__ = [
    "BISECT_TOOLS",
    "BLAME_TOOLS",
    "CRITIC_TOOLS",
    "DEPS_TOOLS",
    "FLAKE_TOOLS",
    "run_oracle",
]


@tool(parse_docstring=True)
@guard
def run_oracle(ref: str, runs: int = 1) -> str:
    """Run the test at one commit, in a scratch worktree, and report the outcome.

    The test is the operator's configured command; you choose only the ref.
    Use this to check a single ref. To compare two refs under identical
    conditions use compare_refs, and to search history use walk_back or bisect;
    do not drive a search by hand with this tool.

    Args:
        ref: A commit sha, tag or ref, e.g. "7d2e4b0", "v1.4.0", "HEAD~3".
        runs: How many times to run it, 1 to 10. More than one run shows
            whether the outcome is stable at this ref.
    """
    runs = max(1, min(int(runs), 10))
    worktree = lease("oracle")
    with worktree.lock:
        outcomes = [oracle.run(worktree, ref) for _ in range(runs)]
    statuses = [o.status for o in outcomes]
    return emit(
        {
            "kind": "oracle",
            "ref": ref,
            "sha": outcomes[0].sha,
            "runs": runs,
            "status": statuses[0] if len(set(statuses)) == 1 else "mixed",
            "statuses": statuses,
            "seconds": round(sum(o.seconds for o in outcomes), 2),
            "tail": outcomes[-1].tail,
        }
    )


# Tool sets per role. Only the roles asked to make a pass/fail claim hold an
# oracle tool; `deps` and `blame` read history and can never run code, so a
# claim from them about behaviour is, by construction, an inference.
#
# `bisect` holds exactly one tool. It verifies both endpoints itself, so
# run_oracle and git_log could add nothing — and on three live runs the model
# used them to re-probe the root commit and bisect from there, ignoring the
# proven bound it was given. Two prompt rewrites did not stop that; a tool
# set with one entry does.
FLAKE_TOOLS = [walk_back, compare_refs, run_oracle, git_log]
BISECT_TOOLS = [bisect]
DEPS_TOOLS = [lockfile_diff, diff_hunks, git_log, git_show]
BLAME_TOOLS = [git_show, diff_hunks, git_log]
CRITIC_TOOLS = [
    run_oracle,
    compare_refs,
    walk_back,
    bisect,
    git_log,
    git_show,
    diff_hunks,
    lockfile_diff,
]
