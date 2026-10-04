"""The rename-aware projection, fed diff text directly."""

from __future__ import annotations

import json

import pytest

from swarmr_blame import output
from swarmr_blame.hunks import diff_hunks, project

RENAME_DIFF = """\
diff --git a/shop/cart.py b/shop/cart.py
index 1111111..2222222 100644
--- a/shop/cart.py
+++ b/shop/cart.py
@@ -1,6 +1,6 @@
 class Item:
-    def __init__(self, name, price):
+    def __init__(self, name, unit_price):
         self.name = name
-        self.price = price
+        self.unit_price = unit_price
@@ -12,5 +12,5 @@ class Item:
     def total(self, qty):
-        return self.price * qty
+        return self.unit_price * qty
@@ -30,4 +30,4 @@ def describe(item):
-    return f"{item.name}: {item.price}"
+    return f"{item.name}: {item.unit_price}"
@@ -40,6 +40,6 @@ def checkout(items):
     subtotal = 0
     for item in items:
-        subtotal += item.price
-    return round(subtotal, 2)
+        subtotal += item.unit_price
+    return int(subtotal)
@@ -50,3 +50,3 @@ def checkout(items):
-def with_price(item, price):
-    return item.set_price(price)
+def with_unit_price(item, unit_price):
+    return item.set_unit_price(unit_price)
"""

PLAIN_DIFF = """\
diff --git a/shop/tax.py b/shop/tax.py
index 1111111..2222222 100644
--- a/shop/tax.py
+++ b/shop/tax.py
@@ -1,3 +1,3 @@
 def rate(region):
-    return 0.21
+    return 0.19
@@ -10,3 +10,4 @@ def apply(amount, region):
     r = rate(region)
+    assert r > 0
     return amount * (1 + r)
"""

MIXED_DIFF = """\
diff --git a/uv.lock b/uv.lock
index 1111111..2222222 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,3 +1,3 @@
 [[package]]
 name = "requests"
-version = "2.31.0"
+version = "2.32.0"
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""


def test_compound_identifiers_carrying_the_rename_are_explained() -> None:
    """`with_price` -> `with_unit_price` is the same refactor, not a change
    beyond it; a hunk made only of such lines is dropped with the others."""
    proj = project(RENAME_DIFF)

    assert proj.dropped["hunks"] == 4
    assert all("with_unit_price" not in line for h in proj.kept for line in h["lines"])


def test_pure_rename_hunks_are_dropped_and_the_real_change_marked() -> None:
    proj = project(RENAME_DIFF)

    assert proj.dropped == {
        "hunks": 4,
        "lines": 12,
        "renames": {"price": "unit_price"},
    }
    assert [h["header"] for h in proj.kept] == ["@@ -40,6 +40,6 @@ def checkout(items):"]
    kept = proj.kept[0]
    assert kept["path"] == "shop/cart.py"
    marked = [line for line in kept["lines"] if line.startswith("!")]
    assert marked == ["!-    return round(subtotal, 2)", "!+    return int(subtotal)"]
    assert [line for line in marked if line.startswith("!+")] == [
        "!+    return int(subtotal)"
    ]
    # The rename lines inside the mixed hunk are shown but not marked.
    assert "-        subtotal += item.price" in kept["lines"]
    assert "+        subtotal += item.unit_price" in kept["lines"]
    assert "     subtotal = 0" in kept["lines"]
    assert "4 hunks dropped as pure rename price->unit_price" in proj.note


def test_without_renames_every_hunk_is_kept_and_every_change_marked() -> None:
    proj = project(PLAIN_DIFF)

    assert proj.dropped == {"hunks": 0, "lines": 0, "renames": {}}
    assert len(proj.kept) == 2
    changes = [line for h in proj.kept for line in h["lines"] if not line.startswith(" ")]
    assert changes == ["!-    return 0.21", "!+    return 0.19", "!+    assert r > 0"]


def test_two_occurrences_are_not_a_rename() -> None:
    twice = RENAME_DIFF.split("@@ -12,5")[0]  # only the first hunk: two pairs
    proj = project(twice)

    assert proj.dropped["renames"] == {}
    assert len(proj.kept) == 1
    assert all(line.startswith("!") for line in proj.kept[0]["lines"] if line[:1] != " ")


def test_scope_and_hunkless_changes() -> None:
    everything = project(MIXED_DIFF, "all")
    assert everything.files_changed == 4
    assert [h["path"] for h in everything.kept] == ["uv.lock"]
    assert everything.other == [
        {"path": "logo.png", "change": "binary"},
        {"path": "run.sh", "change": "mode 100644 -> 100755"},
        {"path": "new.py", "change": "renamed from old.py"},
    ]

    code = project(MIXED_DIFF, "code")
    assert code.files_changed == 3
    assert code.kept == []

    lock = project(MIXED_DIFF, "lockfile")
    assert [h["path"] for h in lock.kept] == ["uv.lock"]
    assert lock.other == []


def test_tool_rejects_bad_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "_cache", {})
    assert diff_hunks.invoke({"rev": "-n"}).startswith("tool error")
    assert diff_hunks.invoke({"rev": "a..b", "scope": "lockfiles"}).startswith(
        "tool error: scope must be one of"
    )


def test_tool_diffs_a_commit_against_its_parent(
    repo_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "_cache", {})
    repo_factory(
        [
            ("base", {"a.py": "x = 1\n", "uv.lock": '[[package]]\nname = "a"\n'}),
            ("change", {"a.py": "x = 2\n", "uv.lock": '[[package]]\nname = "b"\n'}),
        ]
    )
    payload = json.loads(diff_hunks.invoke({"rev": "HEAD"}))
    assert payload["kind"] == "hunks"
    assert [h["path"] for h in payload["kept"]] == ["a.py"]
    assert payload["kept"][0]["lines"] == ["!-x = 1", "!+x = 2"]

    root = json.loads(diff_hunks.invoke({"rev": "HEAD^", "scope": "all"}))
    assert sorted(h["path"] for h in root["kept"]) == ["a.py", "uv.lock"]


def test_non_ascii_paths_survive_the_path_filter(repo_factory) -> None:
    """git C-quotes non-ASCII paths unless core.quotepath is off; a quoted
    `--- "a/caf\\303\\251.py"` never matched the path the model asked for."""
    from swarmr_blame.hunks import kept_hunks

    repo_factory([("base", {"café.py": "x = 1\n"}), ("change", {"café.py": "x = 2\n"})])
    payload = json.loads(diff_hunks.invoke({"rev": "HEAD", "path": "café.py"}))
    assert [h["path"] for h in payload["kept"]] == ["café.py"]
    assert [h["path"] for h in kept_hunks("HEAD", "café.py")] == ["café.py"]
