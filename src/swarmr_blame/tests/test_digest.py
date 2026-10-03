"""One-line summaries of this team's tool payloads.

Domain knowledge, which is why it lives with the team: `core` can only report
shape. One case per payload kind, plus the error prefix and the fallbacks.
"""

from __future__ import annotations

import json

from swarmr_blame.digest import digest_result, is_tool_error


def test_log_counts_commits_and_names_the_first_few() -> None:
    payload = json.dumps(
        {
            "kind": "log",
            "count": 4,
            "commits": [
                {"sha": "7d2e4b0abcdef", "subject": "Tidy cart totals"},
                {"sha": "3c9f1e2abcdef", "subject": "Add tax"},
                {"sha": "1111111abcdef", "subject": "c"},
                {"sha": "2222222abcdef", "subject": "d"},
            ],
        }
    )
    assert digest_result(payload) == (
        "4 commits [7d2e4b0 Tidy cart totals, 3c9f1e2 Add tax, 1111111 c +…]"
    )


def test_commit_reads_as_short_sha_and_subject() -> None:
    payload = json.dumps(
        {"kind": "commit", "sha": "7d2e4b0abcdef", "subject": "Tidy cart totals"}
    )
    assert digest_result(payload) == "7d2e4b0 Tidy cart totals"


def test_hunks_report_kept_and_dropped_with_the_rename() -> None:
    payload = json.dumps(
        {
            "kind": "hunks",
            "kept": [{"path": "src/cart.py", "header": "@@", "lines": []}],
            "dropped": {"hunks": 3, "lines": 40, "renames": {"a.py": "b.py"}},
        }
    )
    assert digest_result(payload) == "1 hunks kept, 3 dropped as rename a.py->b.py"


def test_lockfile_diff_lists_the_moves() -> None:
    payload = json.dumps(
        {
            "kind": "lockfile_diff",
            "moved": True,
            "lockfiles": [
                {
                    "path": "uv.lock",
                    "added": {"attrs": "25.1"},
                    "removed": {},
                    "changed": {"pydantic": ["2.10.0", "2.11.0"]},
                }
            ],
        }
    )
    assert digest_result(payload) == (
        "lockfile moved: pydantic 2.10.0->2.11.0, attrs +25.1"
    )


def test_lockfile_diff_without_a_move_says_so() -> None:
    payload = json.dumps({"kind": "lockfile_diff", "moved": False, "lockfiles": []})
    assert digest_result(payload) == "no lockfile change"


def test_walkback_reads_verdict_bound_and_attempts() -> None:
    payload = json.dumps(
        {
            "kind": "walkback",
            "verdict": "bound_found",
            "bound": "3c9f1e2abcdef",
            "head": {"sha": "7d2e4b0abcdef", "status": "fail"},
            "tried": [{"ref": "HEAD"}, {"ref": "HEAD~1"}, {"ref": "HEAD~2"}],
        }
    )
    assert digest_result(payload) == "bound_found, bound 3c9f1e2, 3 tried"


def test_walkback_without_a_bound() -> None:
    payload = json.dumps(
        {"kind": "walkback", "verdict": "never_passed", "bound": None, "tried": [{}] * 5}
    )
    assert digest_result(payload) == "never_passed, bound none, 5 tried"


def test_walkback_unrunnable_names_the_head_status_not_a_count() -> None:
    payload = json.dumps(
        {
            "kind": "walkback",
            "verdict": "unrunnable",
            "bound": None,
            "head": {"status": "unbuildable", "tail": "terraform: not on PATH"},
            "tried": [],
        }
    )
    assert digest_result(payload) == "unrunnable: HEAD unbuildable, nothing probed"


def test_bisect_reads_the_first_bad_object() -> None:
    payload = json.dumps(
        {
            "kind": "bisect",
            "verdict": "found",
            "first_bad": {"sha": "7d2e4b0abcdef", "subject": "Tidy"},
            "steps": [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}],
            "skipped": ["1111111"],
        }
    )
    assert digest_result(payload) == "found first_bad 7d2e4b0 (3 steps, 1 skipped)"


def test_undetermined_bisect_reports_the_candidate_range() -> None:
    payload = json.dumps(
        {
            "kind": "bisect",
            "verdict": "undetermined",
            "first_bad": None,
            "candidates": ["1111111abcdef", "2222222abcdef", "3333333abcdef"],
            "steps": [{}, {}],
            "skipped": ["1111111abcdef", "2222222abcdef"],
        }
    )
    assert digest_result(payload) == (
        "undetermined first_bad 1111111..3333333 (2 steps, 2 skipped)"
    )


def test_comparison_reads_both_splits() -> None:
    payload = json.dumps(
        {
            "kind": "comparison",
            "verdict": "stable",
            "good": {"sha": "3c9f1e2", "pass": 5, "fail": 0},
            "bad": {"sha": "7d2e4b0", "pass": 0, "fail": 5},
        }
    )
    assert digest_result(payload) == "stable good 5/0 bad 0/5"


def test_oracle_reads_status_sha_and_time() -> None:
    payload = json.dumps(
        {"kind": "oracle", "status": "fail", "sha": "7d2e4b0abcdef", "seconds": 3.2}
    )
    assert digest_result(payload) == "fail at 7d2e4b0 (3.2s)"


def test_tool_error_is_recognised_and_shown_verbatim() -> None:
    assert is_tool_error("tool error: unknown ref 'nope'")
    assert not is_tool_error('{"kind": "oracle"}')
    assert digest_result("tool error: unknown ref 'nope'").startswith("tool error")


def test_unknown_kind_falls_back_to_its_shape() -> None:
    payload = json.dumps({"kind": "profile", "head": "x", "branch": "main"})
    assert digest_result(payload) == "profile with branch, head"


def test_truncated_json_is_reported_as_such() -> None:
    assert "truncated json" in digest_result('{"kind": "log", "commits": [{"sh')
