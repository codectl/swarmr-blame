"""Git archaeology team: which commit broke the test, and proof.

A commander delegating to four investigators — flake, bisect, deps, blame —
and a critic that must independently disprove the resulting hypothesis before
it is reported. Input is a failing test; output is the first bad commit, the
hunk, and evidence that it was neither flake nor environment.

The team declares itself here. Deleting this package and its tests removes it
completely: discovery is by entry point, so nothing else in the tree refers to
it, and uninstalling the distribution unregisters it.

Importing this module must stay cheap. It is what `teams --list` and the MCP
server read to publish the team, so anything that pulls in the agent framework
or the model SDK is declared with `Lazy` and loaded on first use. Only the
fields `core` may call before a run — the digest and the error test, both of
which are plain string handling — are imported directly.
"""

from __future__ import annotations

from swarmr.core.team import Lazy, Member, Team

from swarmr_blame.digest import digest_result, is_tool_error
from swarmr_blame.prompts import SWEEP_REQUEST
from swarmr_blame.target import PARAMS

__all__ = ["TEAM"]

_MODULE = "swarmr_blame"

TEAM = Team(
    name="repo_forensics",
    summary="Find the commit that broke a test, and prove it.",
    description=(
        "Given a git repository and a test command — any language: `test` is "
        "the command whose exit code says broken or not, exit 0 means pass — find "
        "the commit that first broke it and prove the cause. A commander delegates to "
        "four "
        "investigators: flake runs the test at HEAD and walks back to a passing "
        "ref, bisect runs git bisect with the test as the oracle, deps checks "
        "whether a lockfile moved in the range, blame reads the first bad commit "
        "with rename-aware diff projection to find the hunk; then a critic "
        "independently reruns the oracle and tries to disprove the hypothesis. "
        "Use for 'this test fails on main and I do not know since when', 'which "
        "commit broke X', 'is this test flaky or really broken', and 'did the "
        "dependency bump cause this'. Executes the repository's own tests in "
        "disposable worktrees under a scratch directory; never modifies the "
        "checkout it is pointed at. Reports a location and a diff hunk, never a "
        "fix. Will report 'never passed' or 'flaky' rather than invent a culprit."
    ),
    build=Lazy(f"{_MODULE}.agent:build"),
    profile=Lazy(f"{_MODULE}.agent:profile_target"),
    params=PARAMS,
    default_request=SWEEP_REQUEST,
    report_tool="file_forensics_report",
    orchestrator="commander",
    audit_agents=("critic",),
    digest=digest_result,
    is_error=is_tool_error,
    render_report=Lazy(f"{_MODULE}.report_tool:render_report_args"),
    members=(
        Member("commander", "orchestrates; holds no repository tools of its own"),
        Member("flake", "does the test fail at HEAD, and pass reliably somewhere before"),
        Member("bisect", "which commit first fails the test"),
        Member("deps", "did a dependency move in the range, and does that explain it"),
        Member(
            "blame", "which hunk in the first bad commit, and what the message claims"
        ),
        Member("critic", "independently reruns the oracle and tries to disprove"),
    ),
    prompt_hint="tests/test_cart.py::test_checkout_total fails on main",
)
