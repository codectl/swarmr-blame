"""Lockfile parsers and comparison over fixture text; the tool over a repo."""

from __future__ import annotations

import json

import pytest

from swarmr_blame import output
from swarmr_blame.lockfiles import (
    compare,
    lockfile_diff,
    requirements_packages,
    terraform_lock_packages,
    uv_lock_packages,
)

UV_LOCK = """\
version = 1
requires-python = ">=3.13"

[[package]]
name = "swarmr-blame"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "pyyaml" },
]

[package.metadata]
requires-dist = [{ name = "pyyaml", specifier = ">=6" }]

[[package]]
name = "PyYAML"
version = "6.0.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://x/pyyaml.tar.gz", hash = "sha256:abc" }
"""

REQUIREMENTS = """\
# pinned by pip-compile
-r base.txt
--index-url https://pypi.org/simple
requests==2.32.0  # via httpx
Pillow[xmp] == 10.4.0 \\
    --hash=sha256:deadbeef
numpy>=1.26
pytest
./vendor/money-1.1.0
git+https://example.com/x/y.git@v2 ; python_version >= "3.13"
"""

TERRAFORM_LOCK = """\
# This file is maintained automatically by "terraform init".
# Manual edits may be lost in future updates.

provider "registry.terraform.io/hashicorp/azurerm" {
  version     = "4.14.0"
  constraints = "~> 4.0"
  hashes = [
    "h1:abc=",
    "zh:0123",
  ]
}

provider "registry.terraform.io/hashicorp/random" {
  version = "3.6.3"
  hashes  = ["h1:def="]
}

provider "registry.opentofu.org/hashicorp/random" {
  constraints = ">= 3.0"
}
"""


def test_uv_lock_packages_normalises_names() -> None:
    assert uv_lock_packages(UV_LOCK) == {"swarmr-blame": "0.1.0", "pyyaml": "6.0.2"}
    assert uv_lock_packages("") == {}
    with pytest.raises(ValueError, match="not valid TOML"):
        uv_lock_packages("[[package]\nname = ")


def test_requirements_packages_keeps_pins_and_flags_floating() -> None:
    assert requirements_packages(REQUIREMENTS) == {
        "requests": "2.32.0",
        "pillow": "10.4.0",
        "numpy": "unpinned",
        "pytest": "unpinned",
        "./vendor/money-1.1.0": "path",
        "git+https://example.com/x/y.git@v2": "url",
    }


def test_terraform_lock_packages_keys_on_the_full_address() -> None:
    assert terraform_lock_packages(TERRAFORM_LOCK) == {
        "registry.terraform.io/hashicorp/azurerm": "4.14.0",
        "registry.terraform.io/hashicorp/random": "3.6.3",
        "registry.opentofu.org/hashicorp/random": "unpinned",
    }
    header_only = "\n".join(TERRAFORM_LOCK.splitlines()[:2]) + "\n"
    assert terraform_lock_packages(header_only) == {}
    assert terraform_lock_packages("") == {}


def test_compare_reports_each_movement_once() -> None:
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "2.1", "d": "4"}
    assert compare(before, after) == {
        "added": {"d": "4"},
        "removed": {"c": "3"},
        "changed": {"b": ["2", "2.1"]},
    }
    assert compare(before, before) == {"added": {}, "removed": {}, "changed": {}}


def test_tool_reports_moved_pins_and_absence(
    repo_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "_cache", {})
    repo_factory(
        [
            ("no lockfile yet", {"a.py": "x = 1\n"}),
            ("add lock", {"uv.lock": '[[package]]\nname = "a"\nversion = "1"\n'}),
            (
                "bump",
                {
                    "uv.lock": '[[package]]\nname = "a"\nversion = "2"\n',
                    "sub/requirements.txt": "b==1\n",
                },
            ),
        ]
    )
    bump = json.loads(lockfile_diff.invoke({"rev_range": "HEAD"}))
    assert bump["kind"] == "lockfile_diff"
    assert bump["moved"] is True
    assert bump["lockfiles"] == [
        {
            "path": "sub/requirements.txt",
            "note": "created in this range",
            "added": {"b": "1"},
            "removed": {},
            "changed": {},
            "moved": True,
        },
        {
            "path": "uv.lock",
            "added": {},
            "removed": {},
            "changed": {"a": ["1", "2"]},
            "moved": True,
        },
    ]

    nothing = json.loads(lockfile_diff.invoke({"rev_range": "HEAD~2"}))
    assert nothing["moved"] is False
    assert nothing["lockfiles"] == []
    assert "uv.lock" in nothing["note"] and "requirements.txt" in nothing["note"]
    assert ".terraform.lock.hcl" in nothing["note"]

    assert lockfile_diff.invoke({"rev_range": "HEAD...main"}).startswith("tool error")


def test_tool_reports_a_provider_bump(repo_factory) -> None:
    bumped = TERRAFORM_LOCK.replace('version     = "4.14.0"', 'version     = "4.15.0"')
    repo_factory(
        [
            ("lock providers", {"main.tf": "", ".terraform.lock.hcl": TERRAFORM_LOCK}),
            ("bump azurerm", {".terraform.lock.hcl": bumped}),
        ]
    )
    bump = json.loads(lockfile_diff.invoke({"rev_range": "HEAD"}))
    assert bump["moved"] is True
    assert bump["lockfiles"] == [
        {
            "path": ".terraform.lock.hcl",
            "added": {},
            "removed": {},
            "changed": {"registry.terraform.io/hashicorp/azurerm": ["4.14.0", "4.15.0"]},
            "moved": True,
        }
    ]
