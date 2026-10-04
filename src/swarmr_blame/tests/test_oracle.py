"""oracle: one ref in, one of five outcomes out, every one reached for real."""

from __future__ import annotations

from swarmr_blame import oracle
from swarmr_blame.repo import git
from swarmr_blame.sandbox import Worktree
from swarmr_blame.tests.demo_project import (
    CALC_BROKEN,
    CHECK_HANG,
    CHECK_SKIP,
    project,
)


def _shas() -> list[str]:
    return git(["rev-list", "--reverse", "HEAD"]).split()


def test_pass_then_fail_across_the_break(repo_factory) -> None:
    repo_factory(
        [
            ("add calc", project()),
            ("break total", {"calc.py": CALC_BROKEN}),
        ]
    )
    shas = _shas()
    with Worktree.create("or-passfail") as wt:
        good = oracle.run(wt, shas[0])
        bad = oracle.run(wt, "HEAD")
    assert (good.sha, good.status, good.bisect_code) == (shas[0], "pass", 0)
    assert (bad.sha, bad.status, bad.bisect_code) == (shas[1], "fail", 1)
    assert good.seconds > 0


def test_absent_when_a_named_file_is_not_at_the_ref(repo_factory) -> None:
    repo_factory([("add calc only", project(check=None)), ("add check", project())])
    shas = _shas()
    with Worktree.create("or-absent") as wt:
        out = oracle.run(wt, shas[0])
    assert out.status == "absent"
    assert out.bisect_code == 125
    assert "check.py" in out.tail


def test_unbuildable_on_exit_125(repo_factory) -> None:
    repo_factory([("cannot judge", project(check=CHECK_SKIP))])
    with Worktree.create("or-skip") as wt:
        out = oracle.run(wt, "HEAD")
    assert out.status == "unbuildable"
    assert out.bisect_code == 125


def test_timeout_when_the_command_outlives_the_limit(repo_factory) -> None:
    repo_factory([("hang", project(check=CHECK_HANG))])
    with Worktree.create("or-timeout") as wt:
        out = oracle.run(wt, "HEAD", timeout=1)
    assert out.status == "timeout"
    assert out.bisect_code == 125
    assert "1s" in out.tail


_SETUP_THAT_VENDORS_A_LOCKFILE = (
    "from pathlib import Path\n"
    "Path('node_modules/dep').mkdir(parents=True, exist_ok=True)\n"
    "Path('node_modules/dep/package-lock.json').write_text('{}')\n"
    "Path('setup.log').open('a').write('ran\\n')\n"
)


def test_setup_runs_once_per_tracked_lockfile_state(repo_factory) -> None:
    """A lockfile a setup command vendors into the tree never re-triggers setup;
    a tracked lockfile changing between refs does."""
    import shlex
    import sys

    repo_factory(
        [
            (
                "add calc",
                {
                    **project(),
                    "deps.py": _SETUP_THAT_VENDORS_A_LOCKFILE,
                    "uv.lock": "v1\n",
                },
            ),
            ("bump dep", {"uv.lock": "v2\n"}),
        ],
        setup=f"{shlex.quote(sys.executable)} deps.py",
    )
    shas = _shas()
    with Worktree.create("or-setup") as wt:
        oracle.run(wt, shas[0])
        oracle.run(wt, shas[0])
        assert (wt.path / "setup.log").read_text() == "ran\n"
        assert (wt.path / "node_modules/dep/package-lock.json").exists()
        oracle.run(wt, shas[1])
        assert (wt.path / "setup.log").read_text() == "ran\nran\n"


def test_unbuildable_when_the_command_is_not_on_path(
    repo_factory,
) -> None:
    repo_factory([("add calc", project())], test="no-such-tool-7f3a check.py")
    with Worktree.create("or-nopath") as wt:
        out = oracle.run(wt, "HEAD")
    assert out.status == "unbuildable"
    assert "no-such-tool-7f3a" in out.tail
    assert "PATH" in out.tail
