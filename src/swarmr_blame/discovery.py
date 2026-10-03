"""Runtime profiling of the target repository.

Nothing about the target repository is baked into the prompts. This module asks
git what it is looking at and renders the facts investigators need: where HEAD
is, how deep the history goes, which test command the operator configured and
whether the files it names are committed at HEAD, which lockfiles would make
setup run again, which tags could serve as a passing bound, and which CI files
exist so nobody cites a CI result the team cannot read.

Every call here is a read of the checkout's history. It runs once at startup,
before the first token. Facts are measured at HEAD, not in the working tree:
the team tests commits, so what is committed is what counts, and the only
working-tree fact reported is that it differs from HEAD.

The test and setup commands come from the run's target (`target.py`), already
validated, so what is reported here is what the oracle will run.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from swarmr.core.text import clip

from swarmr_blame.lockfiles import LOCKFILE_NAMES
from swarmr_blame.repo import GitError, RepoError, git
from swarmr_blame.target import current

__all__ = ["RepoProfile", "banner", "profile_repo", "render_facts", "render_mechanics"]

# Fixed paths a CI system reads. Workflow files under .github/workflows are
# matched by directory because their names are free-form.
_CI_FIXED = (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml")
_WORKFLOW_DIR = ".github/workflows"
_WORKFLOW_SUFFIXES = (".yml", ".yaml")

_TAG_COUNT = 5
_BANNER_TEST_WIDTH = 40

# `scheme://user:token@host/...` -> `scheme://host/...`. Only the userinfo is
# touched; the rest of the URL is reported as git holds it.
_URL_CREDENTIALS = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]+@")


@dataclass(slots=True)
class RepoProfile:
    path: str = ""
    head: str = ""
    branch: str = "detached"
    commit_count: int = 0
    dirty: bool = False
    remote: str | None = None
    test: str = ""
    setup: str | None = None
    test_paths: list[str] = field(default_factory=list)
    test_paths_present: list[str] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)
    tools_on_path: dict[str, bool] = field(default_factory=dict)
    tags: list[tuple[str, str]] = field(default_factory=list)
    ci_files: list[str] = field(default_factory=list)

    @property
    def short_head(self) -> str:
        return self.head[:12]

    @property
    def test_paths_missing(self) -> list[str]:
        return [p for p in self.test_paths if p not in self.test_paths_present]


def _strip_credentials(url: str) -> str:
    return _URL_CREDENTIALS.sub(r"\1", url)


def _tree(paths: list[str]) -> tuple[list[str], list[str]]:
    """(lockfiles, ci_files) read off the committed path list."""
    lockfiles: list[str] = []
    ci_files: list[str] = []
    for raw in paths:
        p = PurePosixPath(raw)
        if p.name in LOCKFILE_NAMES:
            lockfiles.append(raw)
        if raw in _CI_FIXED or (
            p.parent.as_posix() == _WORKFLOW_DIR and p.suffix in _WORKFLOW_SUFFIXES
        ):
            ci_files.append(raw)
    return sorted(lockfiles), sorted(ci_files)


def profile_repo() -> RepoProfile:
    target = current()
    test, setup, root = target.test, target.setup, target.repo
    prof = RepoProfile(
        path=str(root), test=test.text, setup=setup.text if setup else None
    )

    try:
        prof.head = git(["rev-parse", "--verify", "HEAD"]).strip()
    except GitError as exc:
        raise RepoError(
            f"{root} has no commits ({exc.stderr}). The team tests commits, so there "
            "is nothing to investigate."
        ) from exc
    branch = git(["symbolic-ref", "--short", "-q", "HEAD"], check=False).strip()
    prof.branch = branch or "detached"
    prof.commit_count = int(git(["rev-list", "--count", "HEAD"]).strip())
    prof.dirty = bool(git(["status", "--porcelain"]).strip())

    remote = git(["remote", "get-url", "origin"], check=False).strip()
    prof.remote = _strip_credentials(remote) if remote else None

    paths = git(["ls-tree", "-r", "--name-only", "-z", "HEAD"]).split("\0")
    committed = {p for p in paths if p}
    prof.lockfiles, prof.ci_files = _tree(sorted(committed))

    prof.test_paths = list(test.paths)
    prof.test_paths_present = [p for p in test.paths if p in committed]

    executables = [test.argv[0]] + ([setup.argv[0]] if setup else [])
    prof.tools_on_path = {
        name: shutil.which(name) is not None for name in dict.fromkeys(executables)
    }

    # creatordate is the tagger date for annotated tags and the commit date
    # for lightweight ones, so one sort order covers both.
    tags = git(
        [
            "for-each-ref",
            "--sort=-creatordate",
            f"--count={_TAG_COUNT}",
            "--format=%(refname:short)%09%(creatordate:short)",
            "refs/tags",
        ]
    )
    prof.tags = [
        (name, date)
        for name, _, date in (line.partition("\t") for line in tags.splitlines())
        if name
    ]
    return prof


def render_facts(prof: RepoProfile) -> str:
    """The <repo> block injected into every prompt. All of it is measured."""
    lines = [
        "<repo>",
        f"Repository at {prof.path}",
        f"HEAD is {prof.branch}@{prof.short_head}, {prof.commit_count} commit(s) "
        "reachable from it.",
    ]
    if prof.remote:
        lines.append(f"Remote origin: {prof.remote}")
    if prof.dirty:
        lines += [
            "",
            "DIRTY: the working tree has uncommitted changes; the team tests "
            "COMMITS, so an uncommitted edit is invisible to it. A failure that "
            "only reproduces in the working tree is outside this team's reach.",
        ]

    lines += [
        "",
        f"Test command: {prof.test}",
        "This is the operator's command and the only definition of 'the test' "
        "the team has: exit 0 is a pass, anything else is a fail. The oracle runs "
        "it as given, from the root of a scratch worktree at each ref.",
    ]
    if prof.setup:
        lines.append(f"Setup command: {prof.setup}")
    else:
        lines.append("Setup command: none; the checkout is used as committed.")
    if prof.test_paths:
        present = ", ".join(prof.test_paths_present) or "none"
        lines.append(f"Files the test command names, committed at HEAD: {present}.")
        if missing := prof.test_paths_missing:
            lines.append(
                f"WARNING: not committed at HEAD: {', '.join(missing)}. The test may "
                "be absent at HEAD too, and an `absent` outcome there is a fact "
                "about the command, not about the code."
            )
    lines.append("")
    if prof.lockfiles:
        lines.append(
            "Lockfiles at HEAD: "
            + ", ".join(prof.lockfiles)
            + ". Setup runs again whenever one of these changes between refs, so a "
            "dependency change is a committed change and shows up in the history."
        )
    else:
        lines.append(
            "Lockfiles at HEAD: none. With no lockfile, a dependency drift between "
            "refs cannot be excluded by this team."
        )
    tools = ", ".join(
        f"{name} {'is on PATH' if found else 'is NOT on PATH'}"
        for name, found in prof.tools_on_path.items()
    )
    lines.append(f"Tools: {tools}.")
    if not all(prof.tools_on_path.values()):
        lines.append(
            "A command whose executable is not on PATH cannot run: every ref will "
            "come back `unbuildable`, which says nothing about the code."
        )

    lines.append("")
    if prof.tags:
        lines.append(f"Latest tags ({len(prof.tags)}):")
        lines += [f"  {name:<24} {date}" for name, date in prof.tags]
        lines.append(
            "A tag is a HINT for a passing bound and nothing more: it must be "
            "verified by the oracle before use. A release tag says the code "
            "shipped, not that this test passed there."
        )
    else:
        lines.append("Tags: none. Any passing bound has to be found by walking back.")

    lines.append("")
    if prof.ci_files:
        lines.append("CI files: " + ", ".join(prof.ci_files))
    else:
        lines.append("CI files: none committed.")
    lines.append(
        "CI status is not readable without forge credentials; never cite a CI "
        "result. The only pass/fail the team knows is the oracle's."
    )
    lines.append("</repo>")
    return "\n".join(lines)


def render_mechanics(prof: RepoProfile) -> str:
    """How to read what the oracle says. Written once, independent of the repo
    except for the lockfile remark, so every role reasons from the same terms."""
    drift = (
        "a lockfile is committed, so a dependency difference between two commits "
        "is itself a committed change"
        if prof.lockfiles
        else "there is no lockfile, so two runs of the same commit may resolve "
        "different dependency versions and a drift cannot be excluded"
    )
    return f"""\
<oracle-mechanics>
Every pass/fail the team reports comes from the oracle, which checks out one
commit in a scratch worktree, runs the setup command if one is configured,
runs the test command, and answers with exactly one of five outcomes:
  * pass         the test command exited 0.
  * fail         the test command exited non-zero.
  * absent       a file the test command names does not exist at this commit:
                 the test is not there yet, so the code was never exercised.
                 absent is NEVER evidence of breakage; it is a hole in the
                 history, not a bad commit.
  * unbuildable  the setup command failed, the test command could not start,
                 or it exited 125 (the `git bisect run` convention for "cannot
                 judge this commit"). Says nothing about the test.
  * timeout      the run exceeded its deadline. Not a fail; a slow or hanging
                 test is its own finding.

A bound is a PASSING LOWER BOUND: a commit where the oracle observed a pass.
It is not "the last good commit". Commits between the bound and the failing
commit may pass or fail; bisect decides, the bound only anchors it.

`compare_refs` interleaves runs of two refs in one worktree, so an
environmental explanation (cache, clock, network, ordering) must name a
difference that survives interleaving. If both refs ran alternately in the
same worktree and only one failed, the environment did not pick the loser.

Bisect may return a RANGE rather than a single commit when the boundary sits
inside unbuildable commits: those are skipped, and the first failing commit
is then only known to lie among them. Report the range as the range.

The team has no network isolation. A test that reaches the network can be
environment-dependent, and a result that differs between runs for that reason
is a property of the test, not of the commit; say so rather than blaming a
commit. On this repository {drift}.
</oracle-mechanics>"""


def banner(prof: RepoProfile) -> str:
    """One line for the run header."""
    return (
        f"{prof.branch}@{prof.short_head}, {prof.commit_count} commits, "
        f"test={clip(prof.test, _BANNER_TEST_WIDTH)}"
    )
