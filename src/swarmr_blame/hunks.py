"""Reading a change as hunks, with the rename noise taken out.

One responsibility: turning a diff into the part of it that did something.
The commit the team is asked to explain is typically "rename price to
unit_price" with one real change hidden among forty mechanical lines, and a
model reading the raw diff anchors on the forty. So the projection pairs each
removed line with the added line in its position, tokenises both, and learns
the substitutions that recur: `price -> unit_price` seen in three or more pairs
is a rename. A hunk whose every change is explained by known renames is dropped
and counted; what is left is what the commit did beyond renaming, and within it
every line a rename does NOT explain is marked `!` so the needle is visible.

The pure function `project` works on diff text so it is tested on strings; the
tool only decides which diff to fetch. Git does the first half of the work:
`-M` pairs moved files, `--ignore-all-space` removes re-indentation.

Dropping is counted, never silent. The output names the renames it learned and
how many hunks and lines they absorbed, so the investigator can ask for
`scope="all"` on a path if it suspects the projection hid something.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.tools import tool

from swarmr_blame.history import span, validate_rev
from swarmr_blame.lockfiles import is_lockfile
from swarmr_blame.output import cached, emit, guard
from swarmr_blame.repo import git

__all__ = ["Projection", "diff_hunks", "kept_hunks", "project"]

Scope = Literal["code", "lockfile", "all"]
_SCOPES = ("code", "lockfile", "all")

# A substitution must recur this often before it counts as a rename. Two
# occurrences is a coincidence a real change can produce (a constant changed
# in a definition and its one use); three is a refactor.
_MIN_PAIRS = 3
_TOKEN = re.compile(r"\w+")


@dataclass(slots=True)
class _Hunk:
    path: str
    header: str
    lines: list[tuple[str, str]] = field(default_factory=list)  # (marker, text)


@dataclass(slots=True)
class _File:
    old_path: str | None = None
    new_path: str | None = None
    binary: bool = False
    modes: tuple[str, str] | None = None
    hunks: list[_Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "?"


@dataclass(slots=True)
class Projection:
    files_changed: int
    kept: list[dict[str, Any]]
    dropped: dict[str, Any]
    other: list[dict[str, str]]
    note: str


def project(diff_text: str, scope: Scope = "all") -> Projection:
    """Project unified diff text to kept hunks, dropped rename hunks and the rest."""
    files = [f for f in _parse(diff_text) if _in_scope(f.path, scope)]
    hunks = [h for f in files for h in f.hunks]
    renames = _learn(hunks)
    kept: list[dict[str, Any]] = []
    dropped_hunks = dropped_lines = 0
    for hunk in hunks:
        changed = [i for i, (m, _) in enumerate(hunk.lines) if m != " "]
        ok: set[int] = set()
        for r, a in _pairs(hunk):
            if _explained(hunk.lines[r][1], hunk.lines[a][1], renames):
                ok |= {r, a}
        if changed and ok.issuperset(changed):
            dropped_hunks += 1
            dropped_lines += len(changed)
            continue
        kept.append(
            {
                "path": hunk.path,
                "header": hunk.header,
                "lines": [
                    f"{'' if m == ' ' or i in ok else '!'}{m}{text}"
                    for i, (m, text) in enumerate(hunk.lines)
                ],
            }
        )
    rename_map = {old: new for old, new in sorted(renames)}
    return Projection(
        files_changed=len(files),
        kept=kept,
        dropped={"hunks": dropped_hunks, "lines": dropped_lines, "renames": rename_map},
        other=[o for f in files for o in _other(f)],
        note=_note(dropped_hunks, rename_map),
    )


def projection(rev: str, path: str | None, scope: Scope) -> Projection:
    """The projection of `rev` (sha or range), optionally limited to `path`."""
    left, right = span(rev)
    args = ["diff", "-M", "--ignore-all-space", "--unified=3", "--no-ext-diff"]
    args += [left, right, "--"]
    if path:
        args.append(path)
    return project(git(args), scope)


def kept_hunks(rev: str, path: str) -> list[dict[str, Any]]:
    """Kept hunks of one file at `rev`, verbatim, or [] on any failure.

    The report's HUNK section is read here rather than filed by the model,
    which paraphrased a diff into prose on a live run. The whole commit is
    projected and then filtered, because a rename is learned from its
    recurrence across every file — diffed alone, the one file holding the real
    change shows every line as unexplained. Any error means "no hunk to show":
    the report must render whatever the repository does.
    """
    try:
        kept = projection(validate_rev(rev), None, "all").kept
    except Exception:
        return []
    return [h for h in kept if h["path"] == path]


def _in_scope(path: str, scope: Scope) -> bool:
    if scope == "all":
        return True
    return is_lockfile(path) == (scope == "lockfile")


def _note(dropped: int, renames: dict[str, str]) -> str:
    if not renames:
        return "No recurring token substitution found; every hunk is shown."
    named = ", ".join(f"{old}->{new}" for old, new in renames.items())
    if not dropped:
        return (
            f"Rename {named} recurs but no hunk was purely that; rename lines are "
            "shown unmarked, lines marked ! are what the commit did beyond renaming."
        )
    return (
        f"{dropped} hunk{'s' if dropped != 1 else ''} dropped as pure rename {named}; "
        "the remaining changes are what the commit did beyond renaming. Lines "
        "marked ! are not explained by the rename."
    )


# --- pairing and explanation -------------------------------------------------


def _pairs(hunk: _Hunk) -> list[tuple[int, int]]:
    """(removed, added) index pairs, positional within each change block.

    A change block is a maximal run of non-context lines. Git lists its removed
    lines before its added lines, so the n-th removal pairs with the n-th
    addition; leftovers on either side are unpaired and never explained.
    """
    pairs: list[tuple[int, int]] = []
    removed: list[int] = []
    added: list[int] = []

    def flush() -> None:
        pairs.extend(zip(removed, added, strict=False))
        removed.clear()
        added.clear()

    for i, (marker, _) in enumerate(hunk.lines):
        if marker == "-":
            removed.append(i)
        elif marker == "+":
            added.append(i)
        else:
            flush()
    flush()
    return pairs


def _split(text: str) -> tuple[list[str], str]:
    """Tokens and the skeleton left when they are removed.

    The skeleton drops whitespace too: the diff was fetched with
    --ignore-all-space, so a re-indented line is already not a change, and the
    comparison here should not resurrect one.
    """
    tokens = _TOKEN.findall(text)
    skeleton = "".join(_TOKEN.sub("", text).split())
    return tokens, skeleton


def _substitution(old: str, new: str) -> tuple[str, str] | None:
    """The single (old_token, new_token) a pair differs by, if that is all."""
    old_tokens, old_skeleton = _split(old)
    new_tokens, new_skeleton = _split(new)
    if len(old_tokens) != len(new_tokens) or old_skeleton != new_skeleton:
        return None
    diffs = {(a, b) for a, b in zip(old_tokens, new_tokens, strict=True) if a != b}
    if len(diffs) != 1:
        return None
    return diffs.pop()


def _learn(hunks: list[_Hunk]) -> set[tuple[str, str]]:
    """Substitutions recurring across the whole diff often enough to be renames."""
    counts: Counter[tuple[str, str]] = Counter()
    for hunk in hunks:
        for r, a in _pairs(hunk):
            if sub := _substitution(hunk.lines[r][1], hunk.lines[a][1]):
                counts[sub] += 1
    return {sub for sub, n in counts.items() if n >= _MIN_PAIRS}


def _explained(old: str, new: str, renames: set[tuple[str, str]]) -> bool:
    """True when every differing token position is a known rename.

    A rename of `price` also rewrites `with_price` and `test_item_price_update`:
    a token pair is explained when applying any learned rename inside the old
    token yields the new one. Whole-token first, so an exact rename never pays
    for the substring scan.
    """
    if not renames:
        return False
    old_tokens, old_skeleton = _split(old)
    new_tokens, new_skeleton = _split(new)
    if len(old_tokens) != len(new_tokens) or old_skeleton != new_skeleton:
        return False
    return all(
        a == b or (a, b) in renames or _embeds(a, b, renames)
        for a, b in zip(old_tokens, new_tokens, strict=True)
    )


def _embeds(a: str, b: str, renames: set[tuple[str, str]]) -> bool:
    return any(old in a and a.replace(old, new) == b for old, new in renames)


# --- parsing ------------------------------------------------------------------


def _parse(text: str) -> list[_File]:
    """Split `git diff` output into files and hunks.

    Paths come from the `---`/`+++` and `rename from`/`rename to` lines rather
    than the `diff --git` header, whose two paths cannot be told apart when one
    contains " b/". Binary files have no `---` line, so for them the header is
    the only source and is split on " b/" as a best effort.
    """
    files: list[_File] = []
    current: _File | None = None
    hunk: _Hunk | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = _File()
            files.append(current)
            hunk = None
            rest = line[len("diff --git ") :]
            if rest.startswith("a/") and " b/" in rest:
                current.old_path, current.new_path = rest[2:].split(" b/", 1)
            continue
        if current is None:
            continue
        if hunk is not None and line[:1] in ("-", "+", " "):
            hunk.lines.append((line[0], line[1:]))
            continue
        if line.startswith("\\ "):
            continue  # "\ No newline at end of file"
        if line.startswith("@@"):
            hunk = _Hunk(current.path, line)
            current.hunks.append(hunk)
        elif line.startswith("--- "):
            current.old_path = _strip_prefix(line[4:], "a/")
        elif line.startswith("+++ "):
            current.new_path = _strip_prefix(line[4:], "b/")
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to ") :]
        elif line.startswith("Binary files "):
            current.binary = True
        elif line.startswith("old mode "):
            current.modes = (line[len("old mode ") :], "")
        elif line.startswith("new mode ") and current.modes:
            current.modes = (current.modes[0], line[len("new mode ") :])
    return files


def _strip_prefix(value: str, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    return value[len(prefix) :] if value.startswith(prefix) else value


def _other(file: _File) -> list[dict[str, str]]:
    """Changes with no hunks to show: binary, mode-only, pure rename, whitespace."""
    if file.binary:
        return [{"path": file.path, "change": "binary"}]
    if file.hunks:
        return []
    if file.modes and file.modes[1]:
        return [{"path": file.path, "change": f"mode {file.modes[0]} -> {file.modes[1]}"}]
    if file.old_path and file.new_path and file.old_path != file.new_path:
        return [{"path": file.path, "change": f"renamed from {file.old_path}"}]
    return [{"path": file.path, "change": "whitespace only"}]


# --- the tool -----------------------------------------------------------------


@tool(parse_docstring=True)
@guard
@cached
def diff_hunks(rev: str, path: str | None = None, scope: str = "code") -> str:
    """The hunks of a commit or range, with pure-rename hunks removed.

    A substitution recurring in three or more changed lines (price ->
    unit_price) is treated as a rename: hunks it fully explains are dropped and
    counted, and in the hunks that remain every line it does NOT explain is
    marked with a leading "!". Cite a marked line, not a file. Whitespace-only
    changes are already excluded.

    Args:
        rev: A sha, diffed against its first parent, or a range "a..b".
        path: Only this file or directory, repository-relative.
        scope: "code" (default) hides lockfiles, "lockfile" shows only them,
            "all" shows everything.
    """
    rev = validate_rev(rev)
    if scope not in _SCOPES:
        raise ValueError(f"scope must be one of {', '.join(_SCOPES)}")
    proj = projection(rev, path, cast(Scope, scope))
    return emit(
        {
            "kind": "hunks",
            "rev": rev,
            "scope": scope,
            "files_changed": proj.files_changed,
            "kept": proj.kept,
            "dropped": proj.dropped,
            "other": proj.other,
            "note": proj.note,
        }
    )
