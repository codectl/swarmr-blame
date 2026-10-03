"""Reading this team's tool results: one-line summary, and what counts as failure.

Domain knowledge, so it lives with the team: only this team knows that a
`bisect` payload's `first_bad` may be a range, that a `comparison` is two
pass/fail counts, that a `walkback` verdict is read off its `bound`, or that
its own tools report trouble by opening with "tool error". `core` gets a
shape-only fallback, marks nothing as an error, and stays domain-free.
"""

from __future__ import annotations

import json
from typing import Any

from swarmr.core.text import clip

__all__ = ["digest_result", "is_tool_error"]

# How this team's tools say "that did not work". `guard` writes it.
_ERROR_PREFIXES = ("tool error",)


def is_tool_error(text: str) -> bool:
    """Whether a tool result is a failure rather than an observation."""
    return text.strip().startswith(_ERROR_PREFIXES)


def digest_result(text: str) -> str:
    """One informative line for a repository tool result.

    A byte count plus the first line is worthless here: every JSON result opens
    with "{". What a reader wants is which kind came back and its verdict, the
    sha it points at, or the error.
    """
    stripped = text.strip()
    if not stripped:
        return "empty"
    if is_tool_error(stripped):
        return clip(stripped, 100)
    if stripped.startswith("{"):
        return _digest_json(stripped, len(text))
    return clip(stripped.splitlines()[0], 100)


def _digest_json(stripped: str, size: int) -> str:
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return f"{size}B truncated json"
    kind = str(data.get("kind", ""))
    line = _BY_KIND.get(kind)
    if line is not None:
        return line(data)
    # Shape fallback for a payload this module does not know.
    keys = ", ".join(sorted(k for k in data if k != "kind"))
    return clip(f"{kind or 'object'} with {keys}" if keys else kind or "object", 100)


def _sha(value: Any) -> str:
    return str(value or "")[:7]


def _log(data: dict[str, Any]) -> str:
    commits = data.get("commits") or []
    shown = ", ".join(
        f"{_sha(c.get('sha'))} {clip(c.get('subject', ''), 40)}"
        for c in commits[:3]
        if isinstance(c, dict)
    )
    more = " +…" if len(commits) > 3 else ""
    return f"{len(commits)} commits" + (f" [{shown}{more}]" if shown else "")


def _commit(data: dict[str, Any]) -> str:
    return f"{_sha(data.get('sha'))} {clip(data.get('subject', ''), 80)}".rstrip()


def _hunks(data: dict[str, Any]) -> str:
    kept = data.get("kept") or []
    dropped = data.get("dropped") or {}
    count = dropped.get("hunks", 0) if isinstance(dropped, dict) else dropped
    renames = dropped.get("renames") or {} if isinstance(dropped, dict) else {}
    moved = ", ".join(f"{old}->{new}" for old, new in list(renames.items())[:2])
    line = f"{len(kept)} hunks kept, {count} dropped as rename"
    return f"{line} {moved}" if moved else line


def _lockfile_diff(data: dict[str, Any]) -> str:
    if not data.get("moved"):
        return "no lockfile change"
    moves: list[str] = []
    for lockfile in data.get("lockfiles") or []:
        for name, (old, new) in (lockfile.get("changed") or {}).items():
            moves.append(f"{name} {old}->{new}")
        added, removed = lockfile.get("added") or {}, lockfile.get("removed") or {}
        moves += [f"{name} +{ver}" for name, ver in added.items()]
        moves += [f"{name} -{ver}" for name, ver in removed.items()]
    shown = ", ".join(moves[:3]) + (" +…" if len(moves) > 3 else "")
    return f"lockfile moved: {shown}" if shown else "lockfile moved"


def _walkback(data: dict[str, Any]) -> str:
    verdict = data.get("verdict", "?")
    if verdict == "unrunnable":
        head = data.get("head") or {}
        return f"unrunnable: HEAD {head.get('status', '?')}, nothing probed"
    bound = _sha(data.get("bound")) or "none"
    tried = len(data.get("tried") or [])
    return f"{verdict}, bound {bound}, {tried} tried"


def _bisect(data: dict[str, Any]) -> str:
    first_bad = data.get("first_bad")
    if isinstance(first_bad, dict):
        where = _sha(first_bad.get("sha"))
    elif first_bad:
        where = str(first_bad)
    else:
        candidates = data.get("candidates") or []
        where = f"{_sha(candidates[0])}..{_sha(candidates[-1])}" if candidates else "none"
    steps = len(data.get("steps") or [])
    skipped = len(data.get("skipped") or [])
    verdict = data.get("verdict", "?")
    return f"{verdict} first_bad {where} ({steps} steps, {skipped} skipped)"


def _comparison(data: dict[str, Any]) -> str:
    def counts(side: Any) -> str:
        side = side if isinstance(side, dict) else {}
        return f"{side.get('pass', 0)}/{side.get('fail', 0)}"

    return (
        f"{data.get('verdict', '?')} good {counts(data.get('good'))} "
        f"bad {counts(data.get('bad'))}"
    )


def _oracle(data: dict[str, Any]) -> str:
    sha, seconds = _sha(data.get("sha")), data.get("seconds", 0)
    return f"{data.get('status', '?')} at {sha} ({seconds}s)"


_BY_KIND = {
    "log": _log,
    "commit": _commit,
    "hunks": _hunks,
    "lockfile_diff": _lockfile_diff,
    "walkback": _walkback,
    "bisect": _bisect,
    "comparison": _comparison,
    "oracle": _oracle,
}
