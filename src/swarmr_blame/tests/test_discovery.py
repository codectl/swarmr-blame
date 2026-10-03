"""Repository profiling over throwaway repositories: every field is measured."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from swarmr.core.team import TeamError

from swarmr_blame.discovery import (
    RepoProfile,
    banner,
    profile_repo,
    render_facts,
    render_mechanics,
)
from swarmr_blame.target import activate


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def test_full_profile(repo_factory) -> None:
    path = repo_factory(
        [
            (
                "scaffold",
                {
                    "main.tf": "",
                    ".terraform.lock.hcl": "provider {}\n",
                    "modules/net/uv.lock": "version = 1\n",
                    "tests/nsg.tftest.hcl": 'run "ok" {}\n',
                    ".github/workflows/ci.yml": "on: push\n",
                    ".github/workflows/README.md": "not a workflow\n",
                },
            ),
            ("second", {"main.tf": "x = 1\n"}),
        ],
        test="terraform test -filter=tests/nsg.tftest.hcl",
        setup="terraform init -backend=false",
    )
    _git(path, "tag", "v1.0")

    prof = profile_repo()

    assert prof.path == str(path)
    assert len(prof.head) == 40
    assert prof.branch == "main"
    assert prof.commit_count == 2
    assert prof.dirty is False
    assert prof.remote is None
    assert prof.test == "terraform test -filter=tests/nsg.tftest.hcl"
    assert prof.setup == "terraform init -backend=false"
    assert prof.test_paths == ["tests/nsg.tftest.hcl"]
    assert prof.test_paths_present == ["tests/nsg.tftest.hcl"]
    assert prof.lockfiles == [".terraform.lock.hcl", "modules/net/uv.lock"]
    assert set(prof.tools_on_path) == {"terraform"}
    assert [name for name, _ in prof.tags] == ["v1.0"]
    assert prof.tags[0][1] == "2026-09-30"
    assert prof.ci_files == [".github/workflows/ci.yml"]


def test_bare_profile(repo_factory) -> None:
    repo_factory([("only", {"README": "hello\n"})], test="python3 check.py")
    prof = profile_repo()
    assert prof.test == "python3 check.py"
    assert prof.setup is None
    assert prof.test_paths == ["check.py"]
    assert prof.test_paths_present == []
    assert prof.test_paths_missing == ["check.py"]
    assert prof.lockfiles == []
    assert prof.tools_on_path == {"python3": True}
    assert prof.tags == []
    assert prof.ci_files == []
    assert prof.commit_count == 1


def test_missing_test_command_is_a_team_error(
    repo_factory,
) -> None:
    path = repo_factory([("only", {"README": "hello\n"})])
    with pytest.raises(TeamError, match="`test` is empty"):
        activate({"repo": str(path), "test": "   "})


def test_tool_absent_from_path(repo_factory) -> None:
    repo_factory(
        [("only", {"README": "hello\n"})],
        test="no-such-tool-xyz run",
        setup="python3 -c pass",
    )
    prof = profile_repo()
    assert prof.tools_on_path == {"no-such-tool-xyz": False, "python3": True}
    text = render_facts(prof)
    assert "no-such-tool-xyz is NOT on PATH" in text
    assert "python3 is on PATH" in text
    assert "every ref will come back `unbuildable`" in text


def test_detached_head(repo_factory) -> None:
    path = repo_factory([("a", {"f": "1\n"}), ("b", {"f": "2\n"})])
    _git(path, "checkout", "-q", "HEAD~1")
    prof = profile_repo()
    assert prof.branch == "detached"
    assert prof.commit_count == 1


def test_dirty_is_untracked_or_modified(repo_factory) -> None:
    path = repo_factory([("a", {"f": "1\n"})])
    assert profile_repo().dirty is False
    (path / "scratch.txt").write_text("untracked\n")
    assert profile_repo().dirty is True


def test_remote_credentials_are_stripped(repo_factory) -> None:
    path = repo_factory([("a", {"f": "1\n"})])
    _git(path, "remote", "add", "origin", "https://user:token@github.com/x/y.git")
    prof = profile_repo()
    assert prof.remote == "https://github.com/x/y.git"
    assert "token" not in render_facts(prof)


def test_lockfiles_are_every_tracked_match(repo_factory) -> None:
    repo_factory(
        [
            (
                "a",
                {
                    "requirements.txt": "x\n",
                    "requirements.lock": "x==8\n",
                    "web/package-lock.json": "{}\n",
                    "notes/requirements.txt.bak": "not a lockfile\n",
                },
            )
        ]
    )
    prof = profile_repo()
    assert prof.lockfiles == [
        "requirements.lock",
        "requirements.txt",
        "web/package-lock.json",
    ]


def test_facts_are_measured_in_the_commit_not_the_tree(repo_factory) -> None:
    path = repo_factory([("a", {"README": "x\n"})])
    (path / "uv.lock").write_text("version = 1\n")
    (path / "check.py").write_text("")
    prof = profile_repo()
    assert prof.lockfiles == []
    assert prof.test_paths_present == []
    assert prof.dirty is True


def test_render_facts_tagged_clean(repo_factory) -> None:
    path = repo_factory(
        [("a", {"uv.lock": "v\n", "tests/test_a.py": "\n"})],
        test="uv run pytest tests/test_a.py::test_x",
        setup="uv sync --frozen",
    )
    _git(path, "tag", "v2.0")
    text = render_facts(profile_repo())
    assert text.startswith("<repo>")
    assert text.endswith("</repo>")
    assert "Test command: uv run pytest tests/test_a.py::test_x" in text
    assert "Setup command: uv sync --frozen" in text
    assert "committed at HEAD: tests/test_a.py." in text
    assert "WARNING" not in text
    assert "Lockfiles at HEAD: uv.lock." in text
    assert "Setup runs again whenever one of these changes between refs" in text
    assert "HINT" in text
    assert "v2.0" in text
    assert "verified by the oracle" in text
    assert "uncommitted changes" not in text
    assert "never cite a CI result" in text


def test_render_facts_dirty_without_lockfile(repo_factory) -> None:
    path = repo_factory([("a", {"README": "x\n"})])
    (path / "wip.py").write_text("x = 1\n")
    text = render_facts(profile_repo())
    assert "the working tree has uncommitted changes" in text
    assert "the team tests COMMITS" in text
    assert "Setup command: none; the checkout is used as committed." in text
    assert "committed at HEAD: none." in text
    assert "WARNING: not committed at HEAD: check.py." in text
    assert "The test may be absent at HEAD too" in text
    assert "no lockfile, a dependency drift between refs cannot be excluded" in text
    assert "HINT" not in text


def test_render_facts_without_named_files(repo_factory) -> None:
    repo_factory([("a", {"go.sum": "\n"})], test="go test ./... -run TestCheckout")
    text = render_facts(profile_repo())
    assert "Files the test command names" not in text
    assert "WARNING" not in text
    assert "Lockfiles at HEAD: go.sum." in text


def test_render_mechanics_names_the_rules() -> None:
    text = render_mechanics(RepoProfile())
    assert text.startswith("<oracle-mechanics>")
    assert text.endswith("</oracle-mechanics>")
    for word in ("pass", "fail", "absent", "unbuildable", "timeout"):
        assert f"* {word}" in text
    assert "exited 0" in text
    assert "exited non-zero" in text
    assert "a file the test command names does not exist" in text
    assert "exited 125" in text
    assert "absent is NEVER evidence of breakage" in text
    assert "PASSING LOWER BOUND" in text
    assert 'not "the last good commit"' in text
    assert "survives interleaving" in text
    assert "RANGE" in text and "unbuildable commits" in text
    assert "no network isolation" in text
    assert "there is no lockfile" in text
    assert "a lockfile is committed" in render_mechanics(
        RepoProfile(lockfiles=["uv.lock"])
    )


def test_banner() -> None:
    prof = RepoProfile(
        branch="main",
        head="0123456789abcdef0123456789abcdef01234567",
        commit_count=42,
        test="go test ./...",
    )
    assert banner(prof) == "main@0123456789ab, 42 commits, test=go test ./..."
    long = RepoProfile(
        branch="detached",
        head="abc",
        test="terraform test -filter=tests/a_very_long_file_name.tftest.hcl -verbose",
    )
    text = banner(long)
    assert text.startswith("detached@abc, 0 commits, test=terraform test")
    assert text.endswith("…")
