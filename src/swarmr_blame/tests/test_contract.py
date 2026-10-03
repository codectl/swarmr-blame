"""What this team promises the shared machinery, and its own guardrails.

`core` renders and records without knowing any role name, so the team must
declare its own vocabulary. Filesystem permissions are the other half of the
non-mutation guarantee: prompts ask, permissions enforce. The tool-set
assertions pin which roles may execute code at all.
"""

from __future__ import annotations

import subprocess
import sys

from swarmr.core.team import Lazy
from swarmr.teams import get
from swarmr.teams import names as registered_names

from swarmr_blame import TEAM
from swarmr_blame.agent import _EVIDENCE_ONLY, _NO_WRITES, _SHA_DEPENDENT
from swarmr_blame.digest import digest_result
from swarmr_blame.tools import (
    BISECT_TOOLS,
    BLAME_TOOLS,
    CRITIC_TOOLS,
    DEPS_TOOLS,
    FLAKE_TOOLS,
)

_ORACLE_TOOLS = {"run_oracle", "compare_refs", "walk_back", "bisect"}


class TestVocabulary:
    def test_team_declares_every_name_core_uses(self) -> None:
        assert TEAM.orchestrator == "commander"
        assert TEAM.audit_agents == ("critic",)
        assert TEAM.report_tool == "file_forensics_report"
        assert TEAM.digest is digest_result

    def test_roster_names_the_specialists(self) -> None:
        names = {member.name for member in TEAM.members}
        assert names == {"commander", "flake", "bisect", "deps", "blame", "critic"}
        assert TEAM.roster().count("\n") == len(TEAM.members) - 1

    def test_registry_exposes_the_team(self) -> None:
        assert get(TEAM.name) is TEAM
        assert TEAM.name in registered_names()

    def test_the_heavyweight_fields_are_declared_lazily(self) -> None:
        """Importing this team must not import its agent stack."""
        for name in ("build", "profile", "render_report"):
            declared = getattr(TEAM, name)
            assert isinstance(declared, Lazy), name
            assert declared.target.startswith("swarmr_blame.")
            declared.resolve()


class TestToolSets:
    def test_only_roles_that_claim_pass_or_fail_can_run_code(self) -> None:
        """`deps` and `blame` read history; a behaviour claim from them is an
        inference by construction, and the prompt says so."""
        for tools in (DEPS_TOOLS, BLAME_TOOLS):
            assert not {tool.name for tool in tools} & _ORACLE_TOOLS

    def test_critic_holds_every_tool(self) -> None:
        every = {
            tool.name
            for tools in (FLAKE_TOOLS, BISECT_TOOLS, DEPS_TOOLS, BLAME_TOOLS)
            for tool in tools
        }
        assert {tool.name for tool in CRITIC_TOOLS} == every

    def test_only_flake_starts_from_the_symptom_alone(self) -> None:
        """Every other specialist is dispatched once and needs the previous
        step's sha, so the harness must not rewrite its briefing. With bisect
        rewritten, it re-probed its own endpoints; with blame rewritten, it
        projected five commits to find the one it was sent."""
        roster = {member.name for member in TEAM.members} - {TEAM.orchestrator}
        assert set(_SHA_DEPENDENT) == roster - {"flake"}
        assert set(TEAM.audit_agents) <= set(_SHA_DEPENDENT)

    def test_no_tool_can_mutate_the_checkout(self) -> None:
        names = {tool.name for tool in CRITIC_TOOLS}
        assert not {"git_commit", "git_checkout", "git_reset", "git_push"} & names

    def test_no_tool_takes_a_command_or_test_argument(self) -> None:
        """The test command is the operator's (`test` param); the model chooses
        refs and counts only. A `command` or `test_id` argument anywhere would
        put model output on an argv, which is the shell this team does not have."""
        for tool in CRITIC_TOOLS:
            fields = set(tool.args)
            assert not fields & {"command", "cmd", "test_id", "test", "script"}, tool.name
            assert fields <= {
                "good",
                "bad",
                "ref",
                "runs",
                "rev",
                "rev_range",
                "path",
                "scope",
                "limit",
                "sha",
            }, (tool.name, fields)


class TestPermissions:
    def test_orchestrator_and_critic_grant_no_write_anywhere(self) -> None:
        assert [(r.operations, r.paths, r.mode) for r in _NO_WRITES] == [
            (["write"], ["/**"], "deny")
        ]

    def test_investigators_allow_evidence_before_the_catch_all_deny(self) -> None:
        """Rules are first-match-wins, so the order is the policy."""
        assert [r.mode for r in _EVIDENCE_ONLY] == ["allow", "deny"]
        assert _EVIDENCE_ONLY[0].paths == ["/evidence/**", "/**/evidence/**"]
        assert _EVIDENCE_ONLY[1].paths == ["/**"]
        assert all(rule.operations == ["write"] for rule in _EVIDENCE_ONLY)


def test_publishing_the_mcp_surface_imports_no_team_implementation() -> None:
    """A subprocess, because this process has already imported the agent module."""
    probe = (
        "import sys\n"
        "from swarmr.server import build_server\n"
        "build_server()\n"
        "heavy = [m for m in sys.modules if m.endswith('swarmr_blame.agent')]\n"
        "print(heavy, len(sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded, _, count = done.stdout.strip().rpartition(" ")
    assert loaded == "[]", done.stdout
    assert int(count) < 1500, done.stdout
