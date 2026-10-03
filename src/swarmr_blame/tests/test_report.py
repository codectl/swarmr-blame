"""What reaches the caller, and what is suppressed on the way.

The report is rendered from the tool call's arguments, so these tests exercise
the one path a filed report can take. The redaction policy is the point: a
sentence naming the value that would be correct is dropped and labelled, the
hunk is never touched, and a located finding always carries its caveat.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarmr_blame.report_tool import render_report_args
from swarmr_blame.tests.conftest import RepoFactory


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=path, capture_output=True, text=True
    ).stdout.strip()


def test_filed_arguments_render_every_section(repo_factory: RepoFactory) -> None:
    path = repo_factory(
        [
            ("feat", {"src/cart.py": "def total(x):\n    return round(x)\n"}),
            (
                "Tidy cart totals",
                {"src/cart.py": "def total(x):\n    return round(x * 2)\n"},
            ),
        ]
    )
    sha = _head(path)
    text = render_report_args(
        {
            "symptom": "tests/test_cart.py::test_total fails on main",
            "first_bad": f"{sha} Tidy cart totals",
            "cause": "round() now receives the doubled total.",
            "hunk_path": "src/cart.py",
            "evidence": ["bound 3c9f1e2 passes 5/5 (flake)"],
            "critic_ruling": "confirmed",
            "dismissed": ["uv.lock moved two commits before the boundary (deps)"],
        }
    )
    for heading in (
        "SYMPTOM",
        "FIRST BAD COMMIT",
        "CAUSE",
        "HUNK",
        "EVIDENCE",
        "CRITIC RULING",
        "DISMISSED",
    ):
        assert heading in text
    assert f"{sha} Tidy cart totals — Jan Visser" in text
    assert "  src/cart.py @@ -1,2 +1,2 @@" in text
    assert "  !-    return round(x)" in text
    assert "  !+    return round(x * 2)" in text


def test_author_is_read_from_git_not_from_the_filing(repo_factory: RepoFactory) -> None:
    path = repo_factory([("a", {"f": "1"}), ("b", {"f": "2"})])
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=path, capture_output=True, text=True
    ).stdout.strip()
    text = render_report_args(
        {"first_bad": f"{sha} b", "author": "Someone Else <x@example.com>", "cause": "c."}
    )
    assert f"{sha} b — Jan Visser <jan@example.com>" in text
    assert "Someone Else" not in text
    # A range names no single author, and a sha the repo lacks fails soft.
    assert "—" not in render_report_args(
        {"first_bad": f"{sha}..deadbee c", "cause": "c."}
    )
    assert "—" not in render_report_args({"first_bad": "0123456 gone", "cause": "c."})


def test_a_counterfactual_sentence_is_dropped_and_labelled() -> None:
    text = render_report_args(
        {
            "first_bad": "7d2e4b0 Tidy cart totals",
            "cause": (
                "Commit 7d2e4b0 moves round() before the tax multiplication. "
                "It should round the post-tax total instead of the pre-tax one. "
                "The assertion compares against 10.80."
            ),
            "hunk": "",
            "evidence": ["e"],
        }
    )
    assert "moves round() before the tax multiplication." in text
    assert "The assertion compares against 10.80." in text
    assert "post-tax total" not in text
    assert "One sentence omitted" in text


@pytest.mark.parametrize(
    "phrasing",
    [
        "The call uses total instead of total_with_tax.",
        "It should be total_with_tax.",
        "Expected total_with_tax here.",
        "total is a typo for total_with_tax.",
        "The argument should read total_with_tax.",
    ],
)
def test_every_way_of_naming_the_replacement_is_pruned(phrasing: str) -> None:
    text = render_report_args({"cause": phrasing, "evidence": ["e"]})
    assert "total_with_tax" not in text
    assert "One sentence omitted" in text


def test_a_fully_pruned_cause_is_not_an_all_clear() -> None:
    """ "none found" means no breaking commit; a pruned cause must not borrow it."""
    text = render_report_args({"cause": "It should be total_with_tax.", "evidence": []})
    assert "CAUSE\nnone found" not in text
    assert "withheld" in text


def test_the_hunk_is_read_from_git_verbatim_whatever_it_says(
    repo_factory: RepoFactory,
) -> None:
    """The hunk quotes the repository, never the model. A diff line containing
    `should` or `instead` is an observation, so the redaction that governs the
    cause must never reach it, and a filed `hunk` text is ignored."""
    path = repo_factory(
        [
            ("a", {"c.py": "if total should_round:  # instead of ceil\n    pass\n"}),
            ("b", {"c.py": "if total.should_round():\n    pass\n"}),
        ]
    )
    text = render_report_args(
        {
            "first_bad": f"{_head(path)} b",
            "cause": "c.",
            "hunk_path": "c.py",
            "hunk": "PASTED -> PROSE (marked !)",
        }
    )
    assert "  !-if total should_round:  # instead of ceil" in text
    assert "  !+if total.should_round():" in text
    assert "PASTED" not in text
    assert "omitted" not in text


def test_no_hunk_section_without_a_resolvable_hunk(repo_factory: RepoFactory) -> None:
    path = repo_factory([("a", {"c.py": "1\n"}), ("b", {"c.py": "2\n"})])
    sha = _head(path)
    assert "HUNK" not in render_report_args({"first_bad": f"{sha} b", "cause": "c."})
    assert "HUNK" not in render_report_args(
        {"first_bad": f"{sha} b", "cause": "c.", "hunk_path": "missing.py"}
    )
    assert "HUNK" not in render_report_args(
        {"first_bad": "0123456 gone", "cause": "c.", "hunk_path": "c.py"}
    )


def test_empty_cause_reads_as_none_found() -> None:
    assert "none found" in render_report_args({"symptom": "s", "first_bad": ""})
    assert "CAUSE\nnone found" in render_report_args({"cause": "", "first_bad": "a"})


def test_no_first_bad_commit_renders_as_none_found_without_caveat() -> None:
    text = render_report_args({"symptom": "s", "cause": "none found"})
    assert "FIRST BAD COMMIT\n  none found" in text
    assert "Location only" not in text


def test_a_located_finding_always_carries_its_caveat() -> None:
    """The team runs the test; it does not write the fix."""
    text = render_report_args({"first_bad": "7d2e4b0 Tidy cart totals", "cause": "c."})
    assert "Location only" in text
    assert text.rstrip().endswith("before you trust it.")


def test_first_bad_is_collapsed_to_one_line() -> None:
    text = render_report_args({"first_bad": "7d2e4b0\n  Tidy\n  totals", "cause": "c."})
    assert "  7d2e4b0 Tidy totals" in text


def test_a_single_string_evidence_value_is_accepted() -> None:
    text = render_report_args({"cause": "c.", "evidence": "one line only"})
    assert "- one line only" in text
