"""Walking back from HEAD until the test passes: a lower bound for bisect.

One responsibility: producing a commit at which the test is known to pass, and
nothing about what happened between there and HEAD. `bisect` needs a passing
endpoint and the model rarely knows one; this finds it by measurement.

The bound is a PASSING LOWER BOUND, never "the last good commit". The walk
probes HEAD~1, HEAD~2, HEAD~4, HEAD~8 ... and stops at the first pass, so the
commits it jumped over are untested: the break can be anywhere between the
bound and the previous probe. Saying "last good" here would hand the bisect a
conclusion it has not earned, which is why the result spells the distinction
out in `note` and the verdict names a bound, not a culprit.

Doubling rather than stepping because a regression is usually recent and
history is usually long: a break ten commits back costs five probes, a break a
thousand commits back costs eleven, and each probe is a full oracle run.

An `absent` probe means a file the test command names is not at that ref.
The walk jumps to the commit that added the file and probes there: a pass is
the bound, anything else means the test never passed in its lifetime. When
the command names no file there is nowhere to jump, so the walk reports the
same. `unbuildable` and `timeout` probes are recorded and stepped over; they
say nothing about the test.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from langchain_core.tools import tool

from swarmr_blame import oracle
from swarmr_blame.output import emit, guard
from swarmr_blame.repo import git, resolve
from swarmr_blame.sandbox import Worktree, lease
from swarmr_blame.target import current

__all__ = ["Probe", "Verdict", "WalkResult", "probe", "walk_back"]

Verdict = Literal["head_passes", "bound_found", "never_passed", "unrunnable"]

NOTE = (
    "bound is a passing lower bound, not the last good commit: the probes doubled "
    "back from HEAD and stopped at the first pass, so commits between the bound and "
    "the previous probe are untested. Bisect between bound and HEAD to find the break."
)

UNRUNNABLE_NOTE = (
    "the test could not be run at HEAD, so nothing about history was learned and "
    "no probe was made: this is the sandbox or the command, not a commit. The "
    "tail says what failed; fix that (a tool missing from PATH, a setup command, a "
    "file the command names that is not committed) and run again."
)


@dataclass(frozen=True, slots=True)
class Probe:
    ref: str
    sha: str
    status: oracle.Status
    seconds: float
    tail: str = ""


@dataclass(frozen=True, slots=True)
class WalkResult:
    verdict: Verdict
    bound: str | None
    head: Probe
    tried: tuple[Probe, ...]

    def payload(self) -> dict[str, object]:
        unrunnable = self.verdict == "unrunnable"
        return {
            "kind": "walkback",
            "verdict": self.verdict,
            "bound": self.bound,
            "head": _row(self.head, tail=unrunnable),
            "tried": [_row(p, tail=unrunnable) for p in self.tried],
            "note": UNRUNNABLE_NOTE if unrunnable else NOTE,
        }


def _row(p: Probe, *, tail: bool) -> dict[str, object]:
    """One probe as a row; the tail only when it is the finding."""
    row = asdict(p)
    row["seconds"] = round(p.seconds, 2)
    if not tail:
        del row["tail"]
    return row


def _distances(total: int) -> list[int]:
    """1, 2, 4, 8 ... capped so the last probe is the root commit."""
    last = total - 1
    steps: list[int] = []
    d = 1
    while d < last:
        steps.append(d)
        d *= 2
    if last >= 1:
        steps.append(last)
    return steps


def _added_in(head: str, path: str) -> str | None:
    """The commit that introduced `path`, as seen from `head`."""
    out = git(["log", "--format=%H", "--diff-filter=A", "--reverse", head, "--", path])
    return out.split()[0] if out.split() else None


def probe(worktree: Worktree, timeout: int = oracle.ORACLE_TIMEOUT) -> WalkResult:
    """Find a commit at which the test passes, probing back from the target's HEAD.

    Refs are resolved against the target checkout's HEAD, not the worktree's:
    the worktree moves with every probe, and `HEAD~4` relative to wherever the
    previous probe left it would be a different commit each time.
    """
    head_sha = resolve("HEAD")
    at_head = oracle.run(worktree, head_sha, timeout)
    head = Probe("HEAD", at_head.sha, at_head.status, at_head.seconds, at_head.tail)
    if head.status == "pass":
        return WalkResult("head_passes", head.sha, head, ())
    if head.status != "fail":
        # unbuildable, timeout or absent at HEAD: the command did not run, so
        # no probe can mean anything. Walking on would end in "never_passed",
        # which a live run once reported for a test that merely had no
        # `terraform` on the server's PATH — a sandbox problem dressed as a
        # finding about the repository.
        return WalkResult("unrunnable", None, head, ())

    tried: list[Probe] = []
    total = int(git(["rev-list", "--count", head_sha]).strip())
    for distance in _distances(total):
        ref = f"HEAD~{distance}"
        out = oracle.run(worktree, resolve(f"{head_sha}~{distance}"), timeout)
        tried.append(Probe(ref, out.sha, out.status, out.seconds, out.tail))
        if out.status == "pass":
            return WalkResult("bound_found", out.sha, head, tuple(tried))
        if out.status == "absent":
            return _from_birth(worktree, head_sha, head, tried, timeout)
    return _exhausted(head, tried)


def _exhausted(head: Probe, tried: list[Probe]) -> WalkResult:
    """No probe passed. That is `never_passed` only if history was judged at
    all: a walk where every older commit was unbuildable or timed out learned
    nothing, and says so rather than blaming the test."""
    if any(p.status == "fail" for p in tried):
        return WalkResult("never_passed", None, head, tuple(tried))
    return WalkResult("unrunnable", None, head, tuple(tried))


def _from_birth(
    worktree: Worktree,
    head_sha: str,
    head: Probe,
    tried: list[Probe],
    timeout: int,
) -> WalkResult:
    """The test is absent at the last probe: try the commit that added its file.

    The worktree still sits at the absent ref, so the file the oracle found
    missing is the first of the command's paths that is not there.
    """
    path = next(
        (p for p in current().test.paths if not (worktree.path / p).exists()), None
    )
    birth = _added_in(head_sha, path) if path else None
    if birth is None:
        return WalkResult("never_passed", None, head, tuple(tried))
    known = next((p for p in [head, *tried] if p.sha == birth), None)
    if known is None:
        out = oracle.run(worktree, birth, timeout)
        known = Probe(f"added:{path}", out.sha, out.status, out.seconds)
        tried.append(known)
    if known.status == "pass":
        return WalkResult("bound_found", known.sha, head, tuple(tried))
    return WalkResult("never_passed", None, head, tuple(tried))


@tool(parse_docstring=True)
@guard
def walk_back() -> str:
    """Find a commit where the test passes, probing back from HEAD by doubling steps.

    Use this first, before bisect: it produces the passing endpoint bisect needs.
    The result is a passing lower bound, not the last good commit; the commits
    it jumped over are untested. `head_passes` means the test is not failing at
    HEAD at all; `never_passed` means no commit in the test's lifetime passed;
    `unrunnable` means the command could not run (unbuildable, timed out or
    absent at HEAD, or every older commit unbuildable) — a sandbox or command
    problem, not a finding about any commit; its `tail` says what failed.
    The test is the operator's configured command. When a file it names is
    absent further back, the walk jumps to the commit that added it.
    """
    worktree = lease("walkback")
    with worktree.lock:
        result = probe(worktree)
    return emit(result.payload())
