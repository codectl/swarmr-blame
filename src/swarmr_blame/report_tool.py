"""The report the commander files at the end of an investigation.

One responsibility: the shape of a filed report — the tool the commander calls
and the plain-text rendering of its arguments. What may not appear in that text
is policy, and lives in `redaction.py`.

The report is a normal tool rather than `response_format=ToolStrategy(...)`:
that sets `tool_choice="required"`, which the model rejects while thinking is
enabled. A tool call's arguments are visible in the update stream, which makes
the filing deterministic to capture and visible in the delegation trail, and it
stops the commander from ending on the critic's ruling with nothing filed.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool

from swarmr_blame import repo
from swarmr_blame.hunks import kept_hunks
from swarmr_blame.redaction import LOCATION_CAVEAT, OMITTED_NOTE, diagnosis, one_line

__all__ = ["FILE_REPORT_TOOL", "commit_author", "render_hunk", "render_report_args"]

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RANGE = re.compile(r"^[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}$")


def _rev(first_bad: str) -> str:
    """The sha or range opening `first_bad`, else ""."""
    head = first_bad.split(maxsplit=1)[0] if first_bad.strip() else ""
    return head if _SHA.match(head) or _RANGE.match(head) else ""


def render_hunk(first_bad: str, hunk_path: str) -> list[str]:
    """The kept hunks of `hunk_path` at the first-bad rev, verbatim from git.

    Measured, never filed: the model paraphrased a diff into prose on a live
    run. Lines keep diff_hunks' markers, so `!` still means "not explained by
    the rename". [] when there is no rev, no path or no kept hunk.
    """
    rev = _rev(first_bad)
    path = hunk_path.strip()
    if not rev or not path:
        return []
    out: list[str] = []
    for hunk in kept_hunks(rev, path):
        out.append(f"{hunk['path']} {hunk['header']}")
        out.extend(hunk["lines"])
    return out


def commit_author(first_bad: str) -> str:
    """`Name <email>` as git records it for the sha opening `first_bad`, else "".

    Measured, never filed by the model: the commander holds no repo tools and
    once filled this field with a placeholder. A range or anything that is not
    a sha gives "", as does any git failure — the report must render regardless.
    """
    head = first_bad.split(maxsplit=1)[0] if first_bad.strip() else ""
    if not _SHA.match(head):
        return ""
    try:
        return repo.git(["log", "-1", "--format=%an <%ae>", head, "--"]).strip()
    except Exception:
        return ""


def render_report_args(args: dict[str, Any]) -> str:
    """Render filed report arguments as plain text."""
    symptom = str(args.get("symptom") or "").strip()
    lines: list[str] = []
    if symptom:
        lines += ["SYMPTOM", symptom, ""]

    first_bad = one_line(args.get("first_bad"))
    author = commit_author(first_bad)
    if first_bad:
        who = f" — {author}" if author else ""
        lines += ["FIRST BAD COMMIT", f"  {first_bad}{who}"]
    else:
        lines += ["FIRST BAD COMMIT", "  none found"]

    chain, pruned = diagnosis(str(args.get("cause") or "").strip())
    if pruned and not chain:
        chain = "withheld; see HUNK and EVIDENCE"
    lines += ["", "CAUSE", chain or "none found"]
    if pruned:
        lines += [OMITTED_NOTE]

    # Verbatim and never redacted: the hunk quotes the repository, and a diff
    # line is an observation whatever words happen to be in it.
    hunk = render_hunk(first_bad, str(args.get("hunk_path") or ""))
    if hunk:
        lines += ["", "HUNK", *(f"  {line}" for line in hunk)]

    for label, key in (("EVIDENCE", "evidence"), ("DISMISSED", "dismissed")):
        items = args.get(key) or []
        if isinstance(items, str):
            items = [items]
        if items:
            lines += ["", label, *(f"  - {str(item).strip()}" for item in items)]

    if ruling := str(args.get("critic_ruling") or "").strip():
        lines += ["", "CRITIC RULING", f"  {ruling}"]
    if first_bad:
        lines += ["", LOCATION_CAVEAT]
    return "\n".join(lines)


@tool(parse_docstring=True)
def file_forensics_report(
    symptom: str,
    first_bad: str,
    cause: str,
    hunk_path: str,
    evidence: list[str],
    critic_ruling: str = "",
    dismissed: list[str] | None = None,
) -> str:
    """File the final forensics report. Call this exactly once, as your last action.

    Args:
        symptom: Which test and how it fails: the configured test command,
            restated with the observed failure, in one or two sentences.
        first_bad: The first bad commit: its sha and subject, for example
            "7d2e4b0 Tidy cart totals". A range "a1b2c3d..e4f5a6b" when the
            boundary fell inside skipped commits. Exactly "" when no breaking
            commit was found.
        cause: The causal chain from the change in that commit to the observed
            failure, naming the file, the hunk and the values. Give readings,
            never corrections: "round() now receives the pre-tax total" is an
            observation, "should round the post-tax total" is a judgement about
            which side is right, and a sentence naming the value that WOULD be
            correct is dropped from the filed report. Exactly "none found" when
            the test never passed or is flaky.
        hunk_path: The repository-relative path of the file holding the
            responsible hunk, exactly as diff_hunks reported it, for example
            "src/cart.py". The hunk itself is read from the first-bad commit
            and quoted verbatim; do not paste it. "" when there is none.
        evidence: One line per fact, each naming a sha, a count or a line,
            attributed to the specialist that observed it, for example
            "bound 3c9f1e2 passes 2/2, HEAD fails 2/2 (flake)".
        critic_ruling: The critic's verbatim ruling: confirmed, refuted or unproven.
        dismissed: Plausible causes ruled out, each with the reason — a lockfile
            that moved but did not matter, a commit that looked suspicious.
    """
    _ = (symptom, first_bad, cause, hunk_path, evidence, critic_ruling, dismissed)
    # The arguments are the report. Returning a receipt keeps the transcript
    # small; the caller reads the filed arguments from the run stream.
    return "Report filed. Stop here; do not repeat it in prose."


FILE_REPORT_TOOL = file_forensics_report
