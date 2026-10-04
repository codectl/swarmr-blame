"""The oracle: run the operator's test command at one ref and say what happened.

One responsibility: turning a (worktree, ref) into an `Outcome`, and nothing
about which refs to try or what the outcomes mean. `walkback`, `bisection` and
`comparison` compose this; the critic calls it directly. Every pass/fail the
team reports traces back to this function, and the team never decides what
the test is: the caller's `test` command does.

Five outcomes, and they are distinct on purpose:
  pass        the command exited 0
  fail        the command exited non-zero, and the files it names exist
  absent      a file the command names does not exist at this ref: the test
              is not there, so the code under test was never exercised. Never
              "bad" for a bisect.
  unbuildable the setup command failed, or the test command could not start.
              Bisect skips these.
  timeout     the run exceeded BLAME_ORACLE_TIMEOUT

Exit 125 is also `unbuildable`, by the `git bisect run` convention, so an
operator's wrapper script can say "cannot judge this commit" explicitly.

No shell: the command is an argv list, run from the worktree with a scrubbed
environment.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

from swarmr_blame.sandbox import SetupError, Worktree, scrubbed_env
from swarmr_blame.target import current

__all__ = ["Outcome", "Status", "oracle_timeout", "run"]


def oracle_timeout() -> int:
    """Seconds one test run may take; BLAME_ORACLE_TIMEOUT, default 120.

    Read per call, not at import, so a long-lived server honours a retune
    without a restart.
    """
    return int(os.environ.get("BLAME_ORACLE_TIMEOUT", "120"))


Status = Literal["pass", "fail", "absent", "unbuildable", "timeout"]

_SKIP = 125
_TAIL_LINES = 25


@dataclass(frozen=True, slots=True)
class Outcome:
    sha: str
    status: Status
    seconds: float
    tail: str

    @property
    def bisect_code(self) -> int:
        """What `git bisect` expects: 0 good, 1 bad, 125 skip."""
        return {"pass": 0, "fail": 1}.get(self.status, _SKIP)


def run(worktree: Worktree, ref: str, timeout: int | None = None) -> Outcome:
    """Check out `ref` in `worktree`, prepare it if needed, run the test once.

    `timeout` defaults to `oracle_timeout()` at call time.
    """
    if timeout is None:
        timeout = oracle_timeout()
    command = current().test
    sha = worktree.checkout(ref)
    started = time.monotonic()
    missing = [p for p in command.paths if not (worktree.path / p).exists()]
    if missing:
        return Outcome(
            sha,
            "absent",
            time.monotonic() - started,
            f"not at this ref: {', '.join(missing)}",
        )
    try:
        worktree.prepare()
    except SetupError as exc:
        return Outcome(sha, "unbuildable", time.monotonic() - started, str(exc))

    try:
        done = subprocess.run(
            list(command.argv),
            cwd=worktree.path,
            env=scrubbed_env(worktree.path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Outcome(
            sha,
            "unbuildable",
            time.monotonic() - started,
            f"test: `{command.argv[0]}` is not on PATH.",
        )
    except subprocess.TimeoutExpired as exc:
        tail = _tail(str(exc.stdout or "")) or f"no output within {timeout}s"
        return Outcome(sha, "timeout", time.monotonic() - started, tail)

    seconds = time.monotonic() - started
    tail = _tail(done.stdout + "\n" + done.stderr)
    if done.returncode == 0:
        status: Status = "pass"
    elif done.returncode == _SKIP:
        status = "unbuildable"
    else:
        status = "fail"
    return Outcome(sha, status, seconds, tail)


def _tail(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-_TAIL_LINES:])
