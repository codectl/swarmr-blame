"""Shaping what a tool returns: byte-capping, error containment, caching.

One responsibility: everything that happens to a tool result on its way back to
a subagent, and nothing about how the result was obtained. An unhandled
exception inside a tool kills the whole run, and several investigators reading
one repository issue the same `git_log` and `git_show` calls, so every tool
wears the same wrappers.

Oracle runs are deliberately NOT cached: a rerun is the point of a rerun. The
tools that execute the test take `@guard` only.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from swarmr_blame.repo import GitError, RepoError, target
from swarmr_blame.target import TargetError

__all__ = ["MAX_BYTES", "cache_ttl", "cached", "emit", "guard"]

MAX_BYTES = 12_000


def cache_ttl() -> float:
    """Seconds a history read stays memoised; BLAME_CACHE_TTL, default 300.

    Git history does not move during a run, so the window can be long. It
    exists only so a rerun after a refuted hypothesis still sees fresh state.
    Read per call so a long-lived server honours a retune without a restart.
    """
    return float(os.environ.get("BLAME_CACHE_TTL", "300"))


def emit(payload: Any) -> str:
    """Serialise a tool result, byte-capped."""
    text = json.dumps(payload, indent=1, default=str, sort_keys=False)
    if len(text) <= MAX_BYTES:
        return text
    return (
        text[:MAX_BYTES] + f"\n... TRUNCATED at {MAX_BYTES} bytes. Narrow the query "
        "(a single commit, a path filter, a smaller limit) instead of paging."
    )


def guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn any tool failure into a message the model can act on.

    An unhandled exception inside a tool aborts the whole LangGraph run and
    takes every concurrent investigator down with it. A bad ref or a malformed
    test id is feedback, not a fatal condition. Return it as text and let the
    agent retry.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return f"tool error: {exc}"
        except RepoError as exc:
            return f"tool error: {exc}"
        except GitError as exc:
            if "unknown revision" in exc.stderr or "bad revision" in exc.stderr:
                return (
                    f"tool error: unknown revision ({exc.stderr}). Use a sha, tag or "
                    "ref that exists in this repository; git_log lists them."
                )
            return f"tool error: {exc}"
        except Exception as exc:
            return f"tool error: {type(exc).__name__}: {exc}"

    return wrapper


_cache: dict[str, tuple[float, str]] = {}


def cached(fn: Callable[..., str]) -> Callable[..., str]:
    """Memoise identical reads for a window.

    Investigators run concurrently on one repository, so they independently
    issue the same history reads. Each duplicate costs a subprocess and a full
    result's worth of tokens. Set BLAME_CACHE_TTL=0 to disable.

    The key carries the active target's repository: tools take refs, never
    repositories, so `git_log("HEAD", 20)` is the same call text against every
    checkout a long-lived server is pointed at, and the answer must not be.
    Expired entries are swept on every miss so the table is bounded by what
    was asked within one window, not by the life of the process.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        ttl = cache_ttl()
        if ttl <= 0:
            return fn(*args, **kwargs)
        try:
            repo = target()
        except TargetError:
            # Outside a run there is nothing to key on; the tool's own argument
            # checks still answer, and anything that needs git says so itself.
            return fn(*args, **kwargs)
        key = f"{repo}|{fn.__name__}|{args!r}|{sorted(kwargs.items())!r}"
        now = time.monotonic()
        if hit := _cache.get(key):
            stamped, value = hit
            if now - stamped < ttl:
                return value
        for stale in [k for k, (stamped, _) in _cache.items() if now - stamped >= ttl]:
            del _cache[stale]
        value = fn(*args, **kwargs)
        _cache[key] = (now, value)
        return value

    return wrapper
