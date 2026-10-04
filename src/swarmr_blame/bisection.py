"""Driving `git bisect` with the oracle until one commit is the first bad one.

One responsibility: the search between a passing and a failing commit, and
nothing about how those endpoints were found (`walkback`) or what the commit
changed (`history`, `hunks`). The oracle decides good/bad/skip at each step;
git decides which commit to test next, because its choice over merges and
skips is the one thing worth not reimplementing.

Bisect runs IN-PROCESS in the scratch worktree, not through `git bisect run`:
every step is an `Outcome` this code saw, so the steps can be reported, the
skips named, and a hung test killed by the oracle's own timeout. The bisect
state lives in `.git/worktrees/<name>/`, never in the user's checkout, and
`git bisect reset` runs in `finally` so a crash mid-search leaves nothing
behind.

Endpoints are verified first. A "good" that fails or a "bad" that passes
makes the search meaningless, and git would run it anyway and name an
arbitrary commit; `bad_endpoints` says so instead, with both outcomes.

`absent`, `unbuildable` and `timeout` are skips: none of them exercised the
code under test. When the only commits left are skipped, the first bad commit
is one of them or the lowest known bad, and the result lists exactly those
candidates rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

from langchain_core.tools import tool

from swarmr_blame import oracle
from swarmr_blame.output import emit, guard
from swarmr_blame.repo import git
from swarmr_blame.sandbox import Worktree, lease

__all__ = [
    "MAX_STEPS",
    "BisectResult",
    "Commit",
    "Step",
    "Verdict",
    "bisect",
    "bisect_range",
]

# 2^64 commits is more history than exists; the cap only guards a git that
# keeps answering "Bisecting:" when something has gone wrong.
MAX_STEPS = 64

Verdict = Literal["found", "undetermined", "bad_endpoints"]

# Older git prints `is the first bad commit`, newer git quotes the term:
# `is the first 'bad' commit`.
_FIRST_BAD = re.compile(r"^([0-9a-f]{40}) is the first '?bad'? commit", re.MULTILINE)
_ONLY_SKIPPED = "only 'skip'ped commits left"
_MARK = {0: "good", 1: "bad", 125: "skip"}


@dataclass(frozen=True, slots=True)
class Step:
    sha: str
    status: oracle.Status
    seconds: float


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    author: str
    date: str
    subject: str


@dataclass(frozen=True, slots=True)
class BisectResult:
    verdict: Verdict
    good: Step
    bad: Step
    first_bad: Commit | None = None
    steps: tuple[Step, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def skipped(self) -> tuple[str, ...]:
        return tuple(s.sha for s in self.steps if s.status not in ("pass", "fail"))

    def payload(self) -> dict[str, object]:
        return {
            "kind": "bisect",
            "verdict": self.verdict,
            "first_bad": asdict(self.first_bad) if self.first_bad else None,
            "endpoints": {
                "good": {"sha": self.good.sha, "status": self.good.status},
                "bad": {"sha": self.bad.sha, "status": self.bad.status},
            },
            "skipped": list(self.skipped),
            "steps": [_row(s) for s in self.steps],
            "candidates": list(self.candidates),
        }


def _row(s: Step) -> dict[str, object]:
    row = asdict(s)
    row["seconds"] = round(s.seconds, 2)
    return row


def _step(out: oracle.Outcome) -> Step:
    return Step(out.sha, out.status, out.seconds)


def _commit(sha: str) -> Commit:
    out = git(["show", "-s", "--format=%H%n%an%n%aI%n%s", sha])
    full, author, date, subject = out.rstrip("\n").split("\n", 3)
    return Commit(full, author, date, subject)


def _candidates(lowest_bad: str, goods: list[str]) -> tuple[str, ...]:
    """Commits that could still be the first bad one: the lowest known bad and
    everything below it not reachable from a known good."""
    out = git(["rev-list", lowest_bad, "--not", *goods])
    return tuple(out.split())


def bisect_range(
    worktree: Worktree,
    good: str,
    bad: str,
    timeout: int | None = None,
) -> BisectResult:
    """Find the first commit in `good..bad` at which the test fails."""
    good_out = oracle.run(worktree, good, timeout)
    bad_out = oracle.run(worktree, bad, timeout)
    if good_out.status != "pass" or bad_out.status != "fail":
        return BisectResult("bad_endpoints", _step(good_out), _step(bad_out))

    steps: list[Step] = []
    goods = [good_out.sha]
    lowest_bad = bad_out.sha
    try:
        out = git(["bisect", "start", bad_out.sha, good_out.sha], cwd=worktree.path)
        while len(steps) < MAX_STEPS:
            found = _FIRST_BAD.search(out)
            if found:
                return BisectResult(
                    "found",
                    _step(good_out),
                    _step(bad_out),
                    _commit(found[1]),
                    tuple(steps),
                )
            if _ONLY_SKIPPED in out:
                break
            # git has checked out its pick; the oracle re-checks out the same
            # sha, which is a no-op for the bisect state.
            probe = oracle.run(worktree, worktree.head(), timeout)
            steps.append(_step(probe))
            mark = _MARK[probe.bisect_code]
            if mark == "good":
                goods.append(probe.sha)
            elif mark == "bad":
                lowest_bad = probe.sha
            # Exit status 2 is "cannot bisect more", which is an answer, not a
            # failure; the stdout says which.
            out = git(["bisect", mark], cwd=worktree.path, check=False)
        return BisectResult(
            "undetermined",
            _step(good_out),
            _step(bad_out),
            steps=tuple(steps),
            candidates=_candidates(lowest_bad, goods),
        )
    finally:
        git(["bisect", "reset"], cwd=worktree.path, check=False)


@tool(parse_docstring=True)
@guard
def bisect(good: str, bad: str) -> str:
    """Find the first commit at which the test fails, between a passing and a failing ref.

    The test is the operator's configured command. Both endpoints are verified
    with it before the search starts; a good ref that fails or a bad ref that
    passes returns `bad_endpoints` instead of a culprit. Use walk_back to
    obtain the good ref when none is known. The search runs in a scratch
    worktree; the user's checkout is never touched.

    Args:
        good: A sha, tag or ref at which the test passes, e.g. the bound from
            walk_back.
        bad: A sha, tag or ref at which the test fails, usually "HEAD".
    """
    worktree = lease("bisect")
    with worktree.lock:
        result = bisect_range(worktree, good, bad)
    return emit(result.payload())
