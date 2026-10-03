"""What must never survive into a delivered report.

One responsibility: suppression policy. This team runs the test; it does not
write the fix. It can prove that a commit turned a test red and point at the
hunk, but it cannot validate a correction — it never runs the suite against an
edit nobody has made. Prompting alone did not hold that line for the cluster
team (told to verify, the model certified; told to point, it prescribed), so
the policy is enforced in code, on the render path, and the caveats are not
the model's to phrase.

The render path is the only place this can live: the filed report is emitted
from the tool call arguments before the tool body runs, so nothing the tool
itself does can protect what the caller receives.

The hunk is exempt. It quotes the repository verbatim, and a diff line reading
`-    if total should_round:` is an observation, not advice.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["LOCATION_CAVEAT", "OMITTED_NOTE", "diagnosis", "locator", "one_line"]

# Printed by the template, never by the model.
LOCATION_CAVEAT = (
    "  Location only. This team runs the test, it does not write the fix: it has "
    "shown which commit and hunk turned the test red, not what the code should "
    "do. Decide the change yourself and run the suite before you trust it."
)

OMITTED_NOTE = (
    "  One sentence omitted: it named the value or behaviour that would be "
    "correct, which this team cannot validate — it has only ever run the test "
    "against commits that exist. The observed change is in HUNK and EVIDENCE."
)

_PRESCRIPTIVE = re.compile(
    r"\b(replace|rename|change|set|edit|update|correct|fix|remove|delete|add|"
    r"revert|cherry.?pick|rebase|apply|patch|should|must|instead|will)\b",
    re.IGNORECASE,
)

_COUNTERFACTUAL = re.compile(
    r"\b(?:instead of|rather than|should (?:be|read|have|return|use)|expected|"
    r"(?:typo|misspelling|misspelt|misspelled|shorthand)\s+(?:for|of)|"
    r"correct(?:ly)?\s+spell\w*)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.;])\s+")


def one_line(value: Any, limit: int = 120) -> str:
    """Collapse to a single clipped line: a locator, with no room for a recipe.

    Public because the report renders the first-bad sha and the author with it,
    and neither is subject to the prescription check.
    """
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def locator(value: Any) -> str:
    """One line naming where the fault is, or "" when it prescribes a fix instead.

    Dropped whole rather than clipped: a prescription with its verb removed is
    still a prescription, and the commit it points at is reported separately.
    """
    text = one_line(value)
    return "" if text and _PRESCRIPTIVE.search(text) else text


def diagnosis(cause: str) -> tuple[str, bool]:
    """The chain, minus any sentence naming a value that would be correct.

    Returns the kept text and whether anything was dropped, so the caller can
    label the gap instead of silently shortening the finding.

    Sentence granularity on purpose: cutting the clause out of the sentence left
    mangled punctuation or swallowed the trailing half. Nothing is rewritten —
    a sentence is kept whole or dropped whole.
    """
    kept = [s for s in _SENTENCE.split(cause) if not _COUNTERFACTUAL.search(s)]
    joined = " ".join(part.strip() for part in kept if part.strip())
    return joined, len(kept) != len(_SENTENCE.split(cause))
