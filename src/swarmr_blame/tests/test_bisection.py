"""bisection: the oracle drives git bisect to the first bad commit, or says why not."""

from __future__ import annotations

from swarmr_blame.bisection import bisect_range
from swarmr_blame.repo import git
from swarmr_blame.sandbox import Worktree
from swarmr_blame.tests.demo_project import (
    CALC_BROKEN,
    CHECK,
    CHECK_SKIP,
    project,
    touch,
)


def _shas() -> list[str]:
    return git(["rev-list", "--reverse", "HEAD"]).split()


def _bisect_state(wt: Worktree) -> bool:
    return git(["bisect", "log"], cwd=wt.path, check=False).strip() != ""


def test_finds_exactly_the_breaking_commit(repo_factory) -> None:
    repo_factory(
        [
            ("add calc", project()),
            ("note 1", touch(1)),
            ("break total", {"calc.py": CALC_BROKEN}),
            ("note 2", touch(2)),
            ("note 3", touch(3)),
        ]
    )
    shas = _shas()
    with Worktree.create("bs-found") as wt:
        result = bisect_range(wt, shas[0], "HEAD")
        assert not _bisect_state(wt), "worktree left mid-bisect"
    assert result.verdict == "found"
    assert result.first_bad is not None
    assert result.first_bad.sha == shas[2]
    assert result.first_bad.subject == "break total"
    assert result.first_bad.author == "Jan Visser"
    assert result.first_bad.date.startswith("2026-09-30")
    assert result.skipped == ()
    assert {s.sha for s in result.steps} <= set(shas[1:4])
    assert all(s.status in ("pass", "fail") for s in result.steps)
    payload = result.payload()
    assert payload["kind"] == "bisect"
    assert payload["endpoints"] == {
        "good": {"sha": shas[0], "status": "pass"},
        "bad": {"sha": shas[-1], "status": "fail"},
    }


def test_skips_an_unjudgeable_commit_and_still_converges(repo_factory) -> None:
    # The unjudgeable commit sits at the midpoint git tests first; the break is
    # two commits later, so the skip costs a step but not the answer. It is
    # made unjudgeable by check.py exiting 125 (the git bisect run convention),
    # not by breaking calc.py: a traceback is a non-zero exit, hence `fail`,
    # under the generic contract.
    repo_factory(
        [
            ("add calc", project()),
            ("note 1", touch(1)),
            ("cannot judge", {"check.py": CHECK_SKIP}),
            ("restore check", {"check.py": CHECK}),
            ("break total", {"calc.py": CALC_BROKEN}),
            ("note 2", touch(2)),
        ]
    )
    shas = _shas()
    with Worktree.create("bs-skip") as wt:
        result = bisect_range(wt, shas[0], shas[-1])
    assert result.verdict == "found"
    assert result.first_bad is not None
    assert result.first_bad.sha == shas[4]
    assert result.skipped == (shas[2],)
    assert [s.status for s in result.steps if s.sha == shas[2]] == ["unbuildable"]


def test_undetermined_when_the_skip_is_adjacent_to_the_break(repo_factory) -> None:
    repo_factory(
        [
            ("add calc", project()),
            ("cannot judge", {"check.py": CHECK_SKIP}),
            ("break total", {"check.py": CHECK, "calc.py": CALC_BROKEN}),
        ]
    )
    shas = _shas()
    with Worktree.create("bs-undetermined") as wt:
        result = bisect_range(wt, shas[0], shas[2])
        assert not _bisect_state(wt), "worktree left mid-bisect"
    assert result.verdict == "undetermined"
    assert result.first_bad is None
    assert result.skipped == (shas[1],)
    assert set(result.candidates) == {shas[1], shas[2]}
    assert result.payload()["candidates"] == list(result.candidates)


def test_bad_endpoints_refuse_to_search(repo_factory) -> None:
    repo_factory(
        [
            ("add calc", project()),
            ("break total", {"calc.py": CALC_BROKEN}),
        ]
    )
    shas = _shas()
    with Worktree.create("bs-endpoints") as wt:
        result = bisect_range(wt, "HEAD", shas[0])
        assert not _bisect_state(wt)
    assert result.verdict == "bad_endpoints"
    assert (result.good.sha, result.good.status) == (shas[1], "fail")
    assert (result.bad.sha, result.bad.status) == (shas[0], "pass")
    assert result.steps == ()
    assert result.first_bad is None
