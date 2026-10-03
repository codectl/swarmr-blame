"""Assembly of the repository forensics team.

Stock Deep Agents: one `create_deep_agent` commander plus five `SubAgent`
specialists reached through the built-in `task` tool. Findings return as the
task result; the shared filesystem carries only bulk evidence.

Nothing about the target repository is hardcoded. `discovery` profiles the
checkout at build time and the profile is injected into every prompt, so the
same code runs against any repository.
"""

from __future__ import annotations

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from langchain_openai import ChatOpenAI
from swarmr.core.attribution import Attribution
from swarmr.core.middleware import AnnounceName, FirstRoundBriefing
from swarmr.core.model import build_model
from swarmr.core.team import RunContext, TeamBuild

from swarmr_blame import prompts
from swarmr_blame.discovery import (
    banner,
    profile_repo,
    render_facts,
    render_mechanics,
)
from swarmr_blame.report_tool import FILE_REPORT_TOOL
from swarmr_blame.target import activate
from swarmr_blame.tools import (
    BISECT_TOOLS,
    BLAME_TOOLS,
    CRITIC_TOOLS,
    DEPS_TOOLS,
    FLAKE_TOOLS,
)

__all__ = ["build", "profile_target"]

# Rules are first-match-wins, and a subagent that omits `permissions` inherits
# the parent's, so every role states its own. Paths are absolute in the agent's
# virtual filesystem.
_NO_WRITES = [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]

# Investigators may write only their evidence file. Anything else is scratch
# nobody reads, and it clutters the delegation trail.
_EVIDENCE_ONLY = [
    FilesystemPermission(
        operations=["write"], paths=["/evidence/**", "/**/evidence/**"], mode="allow"
    ),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def _subagents(
    model: ChatOpenAI, facts: str, mechanics: str, attribution: Attribution
) -> list[SubAgent]:
    """The five specialists.

    Each `description` is what the commander reads when routing, so it names
    the question the specialist answers and what it needs to be told.
    """
    return [
        {
            "name": "flake",
            "description": (
                "Establishes whether the test fails at HEAD and finds a passing "
                "lower bound by walking back through history with the real test as "
                "the oracle; later, confirms that a bound and a first-bad commit "
                "behave consistently under interleaved reruns in one environment. "
                "Use first, with the test id, and again after bisect to rule out "
                "flakiness."
            ),
            "system_prompt": prompts.flake(facts, mechanics),
            "middleware": [AnnounceName("flake", attribution)],
            "tools": FLAKE_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "bisect",
            "description": (
                "Finds the first commit at which the test fails, between a passing "
                "ref and a failing ref, by running git bisect with the test as the "
                "oracle. Needs the test id, the passing bound and the failing ref. "
                "Reports a single sha, or a range when the boundary sits inside "
                "commits that cannot be built."
            ),
            "system_prompt": prompts.bisect(facts, mechanics),
            "middleware": [AnnounceName("bisect", attribution)],
            "tools": BISECT_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "deps",
            "description": (
                "Checks whether a dependency moved between two refs by projecting "
                "lockfile changes, and whether that movement explains the failure. "
                "Use with the range from the passing bound to the first-bad commit "
                "once bisect has one."
            ),
            "system_prompt": prompts.deps(facts, mechanics),
            "middleware": [AnnounceName("deps", attribution)],
            "tools": DEPS_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "blame",
            "description": (
                "Reads the first-bad commit: message, author, files, and a "
                "rename-aware projection of its diff that drops pure-rename hunks "
                "and marks the lines that changed behaviour. Reports the hunk "
                "responsible and what the message claimed versus what the diff did. "
                "Use with the first-bad sha once bisect has one."
            ),
            "system_prompt": prompts.blame(facts, mechanics),
            "middleware": [AnnounceName("blame", attribution)],
            "tools": BLAME_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "critic",
            "description": (
                "Adjudicates a finished hypothesis by trying to disprove it with its "
                "own oracle runs and history reads. Send ONLY the symptom and the "
                "hypothesis, never the reasoning or the investigators' reports. "
                "Returns RULING: confirmed | refuted | unproven. Must be the last "
                "step of every investigation."
            ),
            "system_prompt": prompts.critic(facts, mechanics),
            "middleware": [AnnounceName("critic", attribution)],
            "tools": CRITIC_TOOLS,
            "model": model,
            # The critic reports a ruling; it has nothing to persist.
            "permissions": _NO_WRITES,
        },
    ]


def profile_target(run: RunContext) -> str:
    """Describe the target repository without building an agent.

    Separate from `build` because building constructs a model client: an
    operator asking "which repository am I pointed at" should not need a model
    API key to find out.
    """
    activate(dict(run.params))
    return banner(profile_repo())


def build(run: RunContext) -> TeamBuild:
    """Profile the repository, then build the team around what is actually there.

    The target is activated first, in this thread: the graph streams from here
    and every tool call it fans out inherits this context, so `repo`, `test`
    and `setup` from the call are what the oracle runs against.
    """
    activate(dict(run.params))
    model = build_model()
    profile = profile_repo()
    facts = render_facts(profile)
    mechanics = render_mechanics(profile)

    graph = create_deep_agent(
        model=model,
        # The commander holds no repository tools on purpose: its context stays
        # clean, and it cannot fabricate an observation it never received. The
        # one tool it does hold files the final report.
        tools=[FILE_REPORT_TOOL],
        system_prompt=prompts.commander(facts, mechanics),
        subagents=_subagents(model, facts, mechanics, run.attribution),
        # Planning belongs in write_todos, which the harness already provides.
        permissions=_NO_WRITES,
        # The first briefing of each specialist is normalised by the harness, so
        # the commander cannot pre-frame a domain it has not looked at yet. The
        # critic is exempt: its payload is a finished hypothesis.
        middleware=[FirstRoundBriefing(exempt=("critic",))],
    )
    return TeamBuild(graph=graph, banner=banner(profile))
