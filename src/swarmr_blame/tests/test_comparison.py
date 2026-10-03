"""comparison: interleaved reruns at two refs, and what the counts may prove."""

from __future__ import annotations

from swarmr_blame.comparison import Comparison, Tally, compare, reading, verdict_of
from swarmr_blame.repo import git
from swarmr_blame.sandbox import Worktree
from swarmr_blame.tests.demo_project import (
    CALC_BROKEN,
    CHECK_FLAKY,
    project,
    touch,
)


def _tally(ref: str, *statuses: str) -> Tally:
    return Tally(ref, "0" * 40, tuple(statuses), 1.0)  # type: ignore[arg-type]


def test_verdict_precedence_over_counts() -> None:
    good, bad = _tally("g", "pass", "pass"), _tally("b", "fail", "fail")
    assert verdict_of(good, bad) == "stable"
    assert verdict_of(bad, good) == "inverted"
    assert verdict_of(_tally("g", "pass", "fail"), bad) == "flaky"
    # A timeout alongside passes is not a mix of pass and fail; it is noted
    # as `other` and does not stop the refs counting as consistently different.
    assert verdict_of(_tally("g", "pass", "timeout"), bad) == "stable"
    assert verdict_of(_tally("g", "unbuildable", "unbuildable"), bad) == "unbuildable"
    assert verdict_of(_tally("g", "absent"), _tally("b", "pass", "fail")) == "unbuildable"
    assert verdict_of(good, good) == "indistinct"
    assert verdict_of(bad, bad) == "indistinct"


def test_reading_states_counts_and_names_the_noisy_ref() -> None:
    c = Comparison(_tally("v1", "pass", "fail"), _tally("HEAD", "fail", "fail"), 2)
    text = reading(c)
    assert "v1 passed 1 and failed 1" in text
    assert "HEAD passed 0 and failed 2" in text
    assert "noisy at v1" in text
    assert c.payload()["good"] == {
        "ref": "v1",
        "sha": "0" * 40,
        "pass": 1,
        "fail": 1,
        "other": 0,
        "statuses": ["pass", "fail"],
        "seconds_total": 1.0,
    }


def test_runs_are_clamped(repo_factory) -> None:
    repo_factory([("add calc", project()), ("note", touch(1))])
    with Worktree.create("cmp-clamp") as wt:
        result = compare(wt, "HEAD~1", "HEAD", runs=0)
    assert result.runs == 1
    assert len(result.good.statuses) == len(result.bad.statuses) == 1
    assert result.verdict == "indistinct"


def test_stable_for_a_real_break(repo_factory) -> None:
    repo_factory(
        [
            ("add calc", project()),
            ("break total", {"calc.py": CALC_BROKEN}),
        ]
    )
    shas = git(["rev-list", "--reverse", "HEAD"]).split()
    with Worktree.create("cmp-stable") as wt:
        result = compare(wt, shas[0], "HEAD", runs=2)
    assert result.verdict == "stable"
    assert (result.good.sha, result.good.passed, result.good.failed) == (shas[0], 2, 0)
    assert (result.bad.sha, result.bad.passed, result.bad.failed) == (shas[1], 0, 2)
    assert result.good.seconds_total > 0
    payload = result.payload()
    assert payload["kind"] == "comparison"
    assert "bisect between them can converge" in str(payload["reading"])


def test_flaky_when_a_ref_both_passes_and_fails(repo_factory) -> None:
    # The counter file beside check.py is untracked, so it survives every
    # checkout and the interleaved runs see invocations 0, 1, 2, 3 in order.
    repo_factory(
        [
            ("add flaky check", project(check=CHECK_FLAKY)),
            ("note", touch(1)),
        ]
    )
    with Worktree.create("cmp-flaky") as wt:
        result = compare(wt, "HEAD~1", "HEAD", runs=2)
    assert result.verdict == "flaky"
    assert result.good.mixed and result.bad.mixed
    assert result.good.statuses == ("fail", "pass")
    assert result.bad.statuses == ("pass", "fail")
