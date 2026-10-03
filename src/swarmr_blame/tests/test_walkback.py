"""walkback: a passing lower bound, found by doubling back from HEAD."""

from __future__ import annotations

from swarmr_blame.repo import git
from swarmr_blame.sandbox import Worktree
from swarmr_blame.tests.demo_project import (
    CALC_BROKEN,
    CHECK,
    CHECK_SKIP,
    project,
    touch,
)
from swarmr_blame.walkback import _distances, probe


def _shas() -> list[str]:
    return git(["rev-list", "--reverse", "HEAD"]).split()


def test_distances_double_and_clamp_to_root() -> None:
    assert _distances(1) == []
    assert _distances(2) == [1]
    assert _distances(5) == [1, 2, 4]
    assert _distances(10) == [1, 2, 4, 8, 9]
    assert _distances(17) == [1, 2, 4, 8, 16]


def test_bound_found_by_doubling(repo_factory) -> None:
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
    with Worktree.create("wb-bound") as wt:
        result = probe(wt)
    assert result.verdict == "bound_found"
    assert result.head.status == "fail"
    assert [p.ref for p in result.tried] == ["HEAD~1", "HEAD~2", "HEAD~4"]
    assert [p.status for p in result.tried] == ["fail", "fail", "pass"]
    assert result.bound == shas[0]
    payload = result.payload()
    assert payload["kind"] == "walkback"
    assert "lower bound" in str(payload["note"])


def test_head_passes_stops_immediately(repo_factory) -> None:
    repo_factory([("add calc", project()), ("note", touch(1))])
    with Worktree.create("wb-head") as wt:
        result = probe(wt)
    assert result.verdict == "head_passes"
    assert result.bound == result.head.sha
    assert result.tried == ()


def test_never_passed_jumps_to_the_commit_that_added_the_test(repo_factory) -> None:
    # calc is already wrong when check.py arrives: the walk finds check.py
    # absent at HEAD~4, jumps to the commit that added it, and it fails there.
    repo_factory(
        [
            ("add calc, already wrong", project(CALC_BROKEN, check=None)),
            ("add check", {"check.py": CHECK}),
            ("note 1", touch(1)),
            ("note 2", touch(2)),
            ("note 3", touch(3)),
        ]
    )
    shas = _shas()
    with Worktree.create("wb-never") as wt:
        result = probe(wt)
    assert result.verdict == "never_passed"
    assert result.bound is None
    statuses = {p.ref: p.status for p in result.tried}
    assert statuses["HEAD~4"] == "absent"
    birth = result.tried[-1]
    assert birth.ref == "added:check.py"
    assert birth.sha == shas[1]
    assert birth.status == "fail"


def test_a_head_that_cannot_run_is_unrunnable_not_never_passed(repo_factory) -> None:
    """The live failure: no `terraform` on the MCP server's PATH made every
    oracle run `unbuildable`, and the walk reported `never_passed` — a sandbox
    problem dressed as a finding about the repository."""
    repo_factory(
        [("add calc", project()), ("note", touch(1))],
        test="no-such-tool-7f3a check.py",
    )
    with Worktree.create("wb-unrun") as wt:
        result = probe(wt)
    assert result.verdict == "unrunnable"
    assert result.bound is None
    assert result.head.status == "unbuildable"
    assert "no-such-tool-7f3a" in result.head.tail
    assert result.tried == (), "nothing may be probed when HEAD cannot run"
    payload = result.payload()
    assert "no-such-tool-7f3a" in str(result.head.tail) and "tail" in str(payload["head"])
    assert "sandbox or the command" in str(payload["note"])


def test_history_that_cannot_be_judged_is_unrunnable_too(repo_factory) -> None:
    """HEAD fails for real, but every older commit says 'skip me' (exit 125):
    nothing about history was judged, so it is not `never_passed`."""
    repo_factory(
        [
            ("add calc, skip", project(CALC_BROKEN, check=CHECK_SKIP)),
            ("note 1", touch(1)),
            ("note 2", touch(2)),
            ("real check", {"check.py": CHECK}),
        ]
    )
    with Worktree.create("wb-unrun2") as wt:
        result = probe(wt)
    assert result.head.status == "fail"
    assert {p.status for p in result.tried} == {"unbuildable"}
    assert result.verdict == "unrunnable"
    assert result.bound is None


def test_absent_jump_lands_on_a_passing_bound(repo_factory) -> None:
    # The test arrived after the module and passed then; the break came later
    # and check.py is absent at HEAD~4, so the bound is the commit that added it.
    repo_factory(
        [
            ("add calc", project(check=None)),
            ("add check", {"check.py": CHECK}),
            ("break total", {"calc.py": CALC_BROKEN}),
            ("note 1", touch(1)),
            ("note 2", touch(2)),
        ]
    )
    shas = _shas()
    with Worktree.create("wb-birth") as wt:
        result = probe(wt)
    assert result.verdict == "bound_found"
    assert result.bound == shas[1]
    refs = [p.ref for p in result.tried]
    assert refs == ["HEAD~1", "HEAD~2", "HEAD~4", "added:check.py"]
    assert [p.status for p in result.tried] == ["fail", "fail", "absent", "pass"]
