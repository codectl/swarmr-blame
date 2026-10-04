"""Running one test at two refs, interleaved, to separate a regression from flake.

One responsibility: deciding whether two refs actually differ on a test, and
nothing about which refs to compare or why they differ. A single run at each
ref proves little: a test that fails one time in three fails at the "bad" ref
and passes at the "good" one by chance often enough to send a bisect chasing
noise. Counts over repeated runs are the evidence a flake claim needs.

Interleaved (good, bad, good, bad ...) in ONE worktree, so the two refs share
an interpreter, a dependency set, a clock and a machine load. Running all of
one ref then all of the other would let a slow disk or a background job
during one half masquerade as a difference between the refs.

The verdict reads the counts, not a single outcome:
  stable       good never fails, bad never passes: the refs differ on this test
  flaky        a ref both passes and fails: the test is noisy at that ref, and
               a bisect on it will not converge
  inverted     good never passes, bad never fails: the endpoints are swapped
  unbuildable  a ref never produced a pass or a fail, so nothing was measured
  indistinct   both refs agree (all pass or all fail): the test does not tell
               them apart
Runs that end `absent`, `unbuildable` or `timeout` count as `other`: they are
reported, and they neither support nor contradict a difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain_core.tools import tool

from swarmr_blame import oracle
from swarmr_blame.output import emit, guard
from swarmr_blame.sandbox import Worktree, lease

__all__ = ["Comparison", "Tally", "Verdict", "compare", "compare_refs", "verdict_of"]

Verdict = Literal["stable", "flaky", "inverted", "unbuildable", "indistinct"]

MAX_RUNS = 10


@dataclass(frozen=True, slots=True)
class Tally:
    ref: str
    sha: str
    statuses: tuple[oracle.Status, ...]
    seconds_total: float

    @property
    def passed(self) -> int:
        return self.statuses.count("pass")

    @property
    def failed(self) -> int:
        return self.statuses.count("fail")

    @property
    def other(self) -> int:
        return len(self.statuses) - self.passed - self.failed

    @property
    def mixed(self) -> bool:
        return self.passed > 0 and self.failed > 0

    @property
    def measured(self) -> bool:
        return self.passed + self.failed > 0

    def payload(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "sha": self.sha,
            "pass": self.passed,
            "fail": self.failed,
            "other": self.other,
            "statuses": list(self.statuses),
            "seconds_total": round(self.seconds_total, 2),
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    good: Tally
    bad: Tally
    runs: int

    @property
    def verdict(self) -> Verdict:
        return verdict_of(self.good, self.bad)

    def payload(self) -> dict[str, object]:
        return {
            "kind": "comparison",
            "verdict": self.verdict,
            "runs": self.runs,
            "good": self.good.payload(),
            "bad": self.bad.payload(),
            "reading": reading(self),
        }


def verdict_of(good: Tally, bad: Tally) -> Verdict:
    if not good.measured or not bad.measured:
        return "unbuildable"
    if good.mixed or bad.mixed:
        return "flaky"
    if good.failed == 0 and bad.passed == 0:
        return "stable"
    if good.passed == 0 and bad.failed == 0:
        return "inverted"
    return "indistinct"


def reading(c: Comparison) -> str:
    """One sentence stating what the counts prove, and nothing they do not."""
    counts = (
        f"over {c.runs} interleaved runs each, {c.good.ref} passed {c.good.passed} and "
        f"failed {c.good.failed}, {c.bad.ref} passed {c.bad.passed} and failed "
        f"{c.bad.failed}"
    )
    match c.verdict:
        case "stable":
            claim = (
                "the refs differ consistently on this test; a bisect between them "
                "can converge"
            )
        case "flaky":
            noisy = " and ".join(t.ref for t in (c.good, c.bad) if t.mixed)
            claim = f"the test is noisy at {noisy}; a single run proves nothing about it"
        case "inverted":
            claim = "the endpoints are the wrong way round: good fails and bad passes"
        case "unbuildable":
            dead = " and ".join(t.ref for t in (c.good, c.bad) if not t.measured)
            claim = f"{dead} never produced a pass or fail, so nothing was measured there"
        case "indistinct":
            claim = "both refs agree, so this test does not tell them apart"
    return f"{counts}: {claim}."


def compare(
    worktree: Worktree,
    good: str,
    bad: str,
    runs: int = 2,
    timeout: int | None = None,
) -> Comparison:
    """Run the test `runs` times at each ref, interleaved, in one worktree."""
    runs = max(1, min(int(runs), MAX_RUNS))
    good_outs: list[oracle.Outcome] = []
    bad_outs: list[oracle.Outcome] = []
    for _ in range(runs):
        good_outs.append(oracle.run(worktree, good, timeout))
        bad_outs.append(oracle.run(worktree, bad, timeout))
    return Comparison(_tally(good, good_outs), _tally(bad, bad_outs), runs)


def _tally(ref: str, outs: list[oracle.Outcome]) -> Tally:
    return Tally(
        ref,
        outs[0].sha,
        tuple(o.status for o in outs),
        sum(o.seconds for o in outs),
    )


@tool(parse_docstring=True)
@guard
def compare_refs(good: str, bad: str, runs: int = 2) -> str:
    """Run the test repeatedly at two refs, interleaved, and say whether they differ.

    The test is the operator's configured command. Use this to tell a
    regression from a flaky test before bisecting, or to confirm a bisect
    result: `stable` means the refs differ consistently, `flaky` means the test
    is noisy and a bisect on it will not converge. Both refs run in one scratch
    worktree under identical conditions.

    Args:
        good: A sha, tag or ref believed to pass, e.g. the bound from walk_back.
        bad: A sha, tag or ref believed to fail, usually "HEAD".
        runs: Runs per ref, 1 to 10. Two interleaved pairs already separate a
            deterministic break from a flaky test; raise it only when a result
            came back "mixed" and you need the ratio.
    """
    worktree = lease("compare")
    with worktree.lock:
        result = compare(worktree, good, bad, runs)
    return emit(result.payload())
