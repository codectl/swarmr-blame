"""Reading commit history: who changed what, when, and what they said.

One responsibility: the two reads every investigator starts from, `git_log`
for "which commits are in this range" and `git_show` for "what did this one
commit claim and touch". Neither returns a diff; `hunks.py` does that, with the
projection a diff needs to be readable.

Output is parsed from fixed-format git output, never from the human-readable
default. Field separators are the ASCII unit and record separators, which no
commit message contains, and file lists are read NUL-terminated so a path with
a tab or a newline in it cannot shift a column.

A revision argument is validated before it reaches git: whitespace or a leading
dash is a model mistake (`-n 5` smuggled into a ref, `HEAD~5 main`), and
rejecting it here is clearer feedback than git's usage text.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from swarmr_blame.output import cached, emit, guard
from swarmr_blame.repo import git

__all__ = ["EMPTY_TREE", "git_log", "git_show", "span", "validate_rev"]

_US, _RS = "\x1f", "\x1e"
_LOG_FORMAT = f"%H{_US}%an{_US}%aI{_US}%s{_US}%b{_RS}"
_COMMIT_FORMAT = f"%H{_US}%P{_US}%an{_US}%aI{_US}%s{_US}%b{_RS}"
_SHORT = 12
_BODY_CLIP = 300
# git's well-known empty tree: what a root commit is diffed against, since it
# has no parent to name.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def validate_rev(value: str) -> str:
    """A sha, ref or range as git accepts it, with option smuggling rejected."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("a revision is required")
    if stripped.startswith("-"):
        raise ValueError(f"{value!r} starts with '-': give a sha, ref or a..b range")
    if any(ch.isspace() for ch in stripped):
        raise ValueError(
            f"{value!r} contains whitespace: give ONE sha, ref or a..b range"
        )
    return stripped


def span(rev: str) -> tuple[str, str]:
    """The two sides a revision argument compares: `a..b`, or a commit and its
    first parent. Shared by every tool that reads a change rather than a commit."""
    rev = validate_rev(rev)
    if "..." in rev:
        raise ValueError("use a..b; a symmetric a...b range has no single 'before'")
    if ".." in rev:
        left, right = rev.split("..", 1)
        if not left or not right:
            raise ValueError(f"{rev!r}: both ends of a..b are required")
        return left, right
    sha, *parents = git(["rev-list", "--parents", "-n", "1", rev, "--"]).split()
    return (parents[0] if parents else EMPTY_TREE), sha


def _records(text: str, fields: int) -> list[list[str]]:
    rows = []
    for record in text.split(_RS):
        if not record.strip():
            continue
        parts = record.lstrip("\n").split(_US, fields - 1)
        parts += [""] * (fields - len(parts))
        rows.append([p.strip("\n") for p in parts])
    return rows


@tool(parse_docstring=True)
@guard
@cached
def git_log(rev_range: str = "HEAD", limit: int = 20, path: str | None = None) -> str:
    """List commits, newest first, one compact row each.

    Use it to see what lies between two refs, to find candidate shas, or to see
    who last touched a path. A row carries the message body clipped; git_show
    gives the full message and the files a commit touched.

    Args:
        rev_range: A ref ("HEAD", "main"), a sha, or a range ("v1.2..HEAD",
            "abc123^..def456"). Ranges exclude the left end.
        limit: Maximum rows. Narrow the range rather than raising this.
        path: Only commits touching this file or directory, repository-relative.
    """
    rev_range = validate_rev(rev_range)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    args = ["log", f"--format={_LOG_FORMAT}", "-n", str(limit), rev_range, "--"]
    if path:
        args.append(path)
    commits = [
        {
            "sha": sha[:_SHORT],
            "author": author,
            "date": date,
            "subject": subject,
            "body": _clip(body),
        }
        for sha, author, date, subject, body in _records(git(args), 5)
    ]
    return emit(
        {"kind": "log", "range": rev_range, "count": len(commits), "commits": commits}
    )


@tool(parse_docstring=True)
@guard
@cached
def git_show(sha: str) -> str:
    """One commit: full message, parents, and every file it touched with stats.

    Use it to read what a commit claimed to do and compare that with the paths
    and line counts it actually changed. Renames are detected and reported with
    the old path. For the content of the change, use diff_hunks.

    Args:
        sha: The commit, as a sha or any ref naming exactly one commit.
    """
    sha = validate_rev(sha)
    rows = _records(git(["log", "-1", f"--format={_COMMIT_FORMAT}", sha, "--"]), 6)
    if not rows:
        raise ValueError(f"{sha!r} names no commit")
    full, parents, author, date, subject, body = rows[0]
    parent_list = parents.split()
    return emit(
        {
            "kind": "commit",
            "sha": full[:_SHORT],
            "parents": [p[:_SHORT] for p in parent_list],
            "author": author,
            "date": date,
            "subject": subject,
            "body": body.strip("\n"),
            "files": _files(full, parent_list),
        }
    )


def _files(sha: str, parents: list[str]) -> list[dict[str, Any]]:
    """Per-file status and line counts, against the first parent.

    `git diff-tree` on a merge commit prints nothing unless told which parent
    to compare with, so the parent is passed explicitly; a root commit is
    compared with the empty tree via `--root`.
    """
    base = ["diff-tree", "--no-commit-id", "-r", "-M", "-z"]
    span = [parents[0], sha] if parents else ["--root", sha]
    statuses = _parse_z(git([*base, "--name-status", *span]))
    counts = _parse_z(git([*base, "--numstat", *span]))
    files = []
    for (code, *paths), (stat, *_) in zip(statuses, counts, strict=True):
        added, deleted, *_ = stat.split("\t")
        entry: dict[str, Any] = {"path": paths[-1], "status": code[0]}
        if code.startswith("R"):
            entry["old_path"] = paths[0]
        # "-" is git's count for binary content; keep it as the string it is
        # rather than pretending a binary blob had zero lines.
        entry["added"] = int(added) if added.isdigit() else added
        entry["deleted"] = int(deleted) if deleted.isdigit() else deleted
        files.append(entry)
    return files


def _parse_z(text: str) -> list[list[str]]:
    """Group NUL-separated `diff-tree -z` output into one list per file.

    Both `--name-status` and `--numstat` emit one leading field followed by one
    path, or two paths for a rename or copy: `R085\\0old\\0new\\0` and
    `3\\t1\\t\\0old\\0new\\0`. A numstat field ending in a tab is the rename form
    with its paths following; otherwise the path is inline after the last tab.
    """
    fields = text.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    out: list[list[str]] = []
    i = 0
    while i < len(fields):
        head = fields[i]
        if head[:1] in "RC" and head[1:].isdigit():
            out.append([head, fields[i + 1], fields[i + 2]])
            i += 3
        elif "\t" in head:
            counts, path = head.rsplit("\t", 1)
            if path:
                out.append([counts, path])
                i += 1
            else:
                out.append([counts, fields[i + 1], fields[i + 2]])
                i += 3
        else:
            out.append([head, fields[i + 1]])
            i += 2
    return out


def _clip(body: str) -> str:
    body = body.strip("\n")
    if len(body) <= _BODY_CLIP:
        return body
    return body[:_BODY_CLIP].rstrip() + " …"
