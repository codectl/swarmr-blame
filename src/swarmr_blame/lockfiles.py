"""Lockfiles: which pinned package versions differ between two refs.

One responsibility: answering "did a dependency move" as a list of package
names and versions rather than as a diff. A lockfile diff is thousands of
hash lines; the question the `deps` investigator has to answer is three dicts:
added, removed, changed. The parsers are pure functions over file text so they
can be tested on a string, and the tool only decides which text to feed them.

Lockfiles with a readable shape are projected to packages: `uv.lock`,
`poetry.lock` and `Cargo.lock` share the TOML `[[package]]` shape,
`requirements*.txt` and `requirements.lock` are pins, `.terraform.lock.hcl` is
provider blocks. The rest (`package-lock.json`, `yarn.lock`, ...) are reported
as moved or not by comparing the blobs.

Absence is a finding: a range that moved no lockfile says so by name, so the
investigator is not left wondering whether the tool looked.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import PurePosixPath
from typing import Any

from langchain_core.tools import tool

from swarmr_blame.history import span, validate_rev
from swarmr_blame.output import cached, emit, guard
from swarmr_blame.repo import git

__all__ = [
    "LOCKFILE_NAMES",
    "compare",
    "is_lockfile",
    "lockfile_diff",
    "requirements_packages",
    "terraform_lock_packages",
    "uv_lock_packages",
]

# Every lockfile the team recognises by basename, in any directory. The sandbox
# hashes these to decide when the setup command must run again.
LOCKFILE_NAMES = frozenset(
    (
        "uv.lock",
        "requirements.lock",
        "requirements.txt",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        ".terraform.lock.hcl",
    )
)

_TOML_PACKAGES = frozenset(("uv.lock", "poetry.lock", "Cargo.lock"))
_TERRAFORM_LOCK = ".terraform.lock.hcl"
_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(==\s*([^\s;#\\]+))?"
)
# `provider "<address>" { ... }`; the body holds no braces of its own, only
# `version`, `constraints` and a `hashes` list.
_PROVIDER_BLOCK = re.compile(r'provider\s+"([^"]+)"\s*\{([^}]*)\}')
_PROVIDER_VERSION = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
_UNPINNED = "unpinned"

Packages = dict[str, str]


def is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name in LOCKFILE_NAMES


def normalise(name: str) -> str:
    """PEP 503 name normalisation, so `Foo_Bar` and `foo-bar` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def uv_lock_packages(text: str) -> Packages:
    """`[[package]]` name -> version, for uv.lock and the lockfiles shaped like it."""
    if not text.strip():
        return {}
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"lockfile is not valid TOML: {exc}") from exc
    return {
        normalise(str(pkg["name"])): str(pkg.get("version", _UNPINNED))
        for pkg in doc.get("package", [])
        if isinstance(pkg, dict) and "name" in pkg
    }


def requirements_packages(text: str) -> Packages:
    """`name==version` pins from a requirements file.

    Comments, blank lines and options (`-r`, `-e`, `--hash`, ...) are skipped.
    A requirement without an exact pin is kept with the version "unpinned",
    because a floating requirement is itself a reason a build moved. A path or
    URL requirement (`./vendor/money-1.1.0`, `git+https://...@v2`) has no name
    to key on, so the whole requirement is the key and the version is its
    kind: swapping `./vendor/money-1.0.0` for `./vendor/money-1.1.0` then shows
    as one removal and one addition rather than as nothing at all.
    """
    packages: Packages = {}
    for raw in _logical_lines(text):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT.match(line)
        if not match or "/" in line.split()[0]:
            kind = "url" if "://" in line else "path"
            packages[line.split(";", 1)[0].strip()] = kind
            continue
        name, _, version = match.groups()
        packages[normalise(name)] = version or _UNPINNED
    return packages


def terraform_lock_packages(text: str) -> Packages:
    """Provider address -> selected version from `.terraform.lock.hcl`.

    The address is kept whole (`registry.terraform.io/hashicorp/azurerm`),
    because the same name under two hosts is two providers. Only `version` is
    read: `constraints` is what was asked for and `hashes` is how the selected
    build was verified, and neither is what moved when a bump breaks a test.
    A block with no version line is kept as "unpinned" rather than skipped.
    """
    packages: Packages = {}
    for address, body in _PROVIDER_BLOCK.findall(text):
        version = _PROVIDER_VERSION.search(body)
        packages[address] = version.group(1) if version else _UNPINNED
    return packages


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations, which pip-compile uses for `--hash` runs."""
    lines: list[str] = []
    pending = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def compare(before: Packages, after: Packages) -> dict[str, Any]:
    """What moved between two package maps."""
    return {
        "added": {n: after[n] for n in sorted(after.keys() - before.keys())},
        "removed": {n: before[n] for n in sorted(before.keys() - after.keys())},
        "changed": {
            n: [before[n], after[n]]
            for n in sorted(before.keys() & after.keys())
            if before[n] != after[n]
        },
    }


def _packages(path: str, text: str) -> Packages | None:
    """Project a lockfile to packages, or None when the format is not parsed."""
    name = PurePosixPath(path).name
    if name in _TOML_PACKAGES:
        return uv_lock_packages(text)
    if name.startswith("requirements"):
        return requirements_packages(text)
    if name == _TERRAFORM_LOCK:
        return terraform_lock_packages(text)
    return None


@tool(parse_docstring=True)
@guard
@cached
def lockfile_diff(rev_range: str) -> str:
    """Which pinned dependency versions differ between two refs.

    Use it before blaming code: a test that starts failing exactly where a
    lockfile moved may be failing because of the dependency, not the commit.
    Every lockfile present at either end is compared; lockfiles with a readable
    shape are reported package by package, other formats only as moved or not.

    Args:
        rev_range: "a..b", or a single sha meaning that commit against its
            first parent.
    """
    rev_range = validate_rev(rev_range)
    left, right = span(rev_range)
    before_paths = _lockfile_paths(left)
    after_paths = _lockfile_paths(right)
    paths = sorted(before_paths | after_paths)
    looked_for = ", ".join(sorted(LOCKFILE_NAMES))
    if not paths:
        return emit(
            {
                "kind": "lockfile_diff",
                "range": rev_range,
                "lockfiles": [],
                "moved": False,
                "note": f"No lockfile at either end of {rev_range}; looked for "
                f"{looked_for} in every directory.",
            }
        )
    lockfiles = []
    moved = False
    for path in paths:
        before = git(["show", f"{left}:{path}"]) if path in before_paths else ""
        after = git(["show", f"{right}:{path}"]) if path in after_paths else ""
        entry: dict[str, Any] = {"path": path}
        if path not in before_paths:
            entry["note"] = "created in this range"
        elif path not in after_paths:
            entry["note"] = "deleted in this range"
        before_pkgs, after_pkgs = _packages(path, before), _packages(path, after)
        if before_pkgs is None or after_pkgs is None:
            entry["parsed"] = False
            entry["moved"] = before != after
        else:
            entry.update(compare(before_pkgs, after_pkgs))
            entry["moved"] = any(entry[k] for k in ("added", "removed", "changed"))
        moved = moved or entry["moved"]
        lockfiles.append(entry)
    note = (
        "A moved pin is a hypothesis, not a cause, until the oracle fails with "
        "the new pin and passes with the old one."
        if moved
        else f"Lockfiles present but unchanged across {rev_range}; dependencies "
        "did not move."
    )
    return emit(
        {
            "kind": "lockfile_diff",
            "range": rev_range,
            "lockfiles": lockfiles,
            "moved": moved,
            "note": note,
        }
    )


def _lockfile_paths(sha: str) -> set[str]:
    listing = git(["ls-tree", "-r", "-z", "--name-only", sha])
    return {p for p in listing.split("\0") if p and is_lockfile(p)}
