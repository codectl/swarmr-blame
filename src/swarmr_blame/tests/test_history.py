"""git_log rows and git_show file lists over a repository built by the fixture."""

from __future__ import annotations

import json

import pytest

from swarmr_blame.history import git_log, git_show, span, validate_rev

CART = "class Item:\n    def __init__(self, price):\n        self.price = price\n"


@pytest.fixture
def history(repo_factory) -> None:
    repo_factory(
        [
            ("add cart", {"shop/cart.py": CART, "README.md": "shop\n"}),
            ("tax table", {"shop/tax.py": "RATE = 0.21\n"}),
            (
                "rename cart to basket\n\nPure rename, no behaviour change.\n",
                {
                    "shop/cart.py": None,
                    "shop/basket.py": CART + "\n    def total(self):\n        return 1\n",
                    "README.md": None,
                },
            ),
        ]
    )


def test_log_rows_are_compact_and_filterable(history: None) -> None:
    payload = json.loads(git_log.invoke({}))
    assert payload["kind"] == "log"
    assert payload["range"] == "HEAD"
    assert payload["count"] == 3
    newest, *_ = payload["commits"]
    assert len(newest["sha"]) == 12
    assert newest["author"] == "Jan Visser"
    assert newest["date"] == "2026-09-30T10:00:00+02:00"
    assert newest["subject"] == "rename cart to basket"
    assert newest["body"] == "Pure rename, no behaviour change."
    assert [c["subject"] for c in payload["commits"]] == [
        "rename cart to basket",
        "tax table",
        "add cart",
    ]

    by_path = json.loads(git_log.invoke({"path": "shop/tax.py"}))
    assert [c["subject"] for c in by_path["commits"]] == ["tax table"]

    limited = json.loads(git_log.invoke({"rev_range": "HEAD~2..HEAD", "limit": 1}))
    assert limited["count"] == 1
    assert limited["commits"][0]["subject"] == "rename cart to basket"


def test_show_lists_files_with_rename_and_counts(history: None) -> None:
    payload = json.loads(git_show.invoke({"sha": "HEAD"}))
    assert payload["kind"] == "commit"
    assert payload["subject"] == "rename cart to basket"
    assert payload["body"] == "Pure rename, no behaviour change."
    assert len(payload["parents"]) == 1
    assert payload["files"] == [
        {"path": "README.md", "status": "D", "added": 0, "deleted": 1},
        {
            "path": "shop/basket.py",
            "status": "R",
            "old_path": "shop/cart.py",
            "added": 3,
            "deleted": 0,
        },
    ]

    root = json.loads(git_show.invoke({"sha": "HEAD~2"}))
    assert root["parents"] == []
    assert [f["status"] for f in root["files"]] == ["A", "A"]


def test_revisions_are_validated_before_git_sees_them(history: None) -> None:
    for bad in ("-n", "HEAD main", "", "  "):
        with pytest.raises(ValueError):
            validate_rev(bad)
    assert git_log.invoke({"rev_range": "--all"}).startswith("tool error")
    assert git_show.invoke({"sha": "nope"}).startswith("tool error: unknown revision")


def test_span_resolves_parent_and_root(history: None) -> None:
    assert span("v1..v2") == ("v1", "v2")
    left, right = span("HEAD")
    assert len(left) == len(right) == 40 and left != right
    root_left, _ = span("HEAD~2")
    assert root_left == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    with pytest.raises(ValueError):
        span("a...b")
