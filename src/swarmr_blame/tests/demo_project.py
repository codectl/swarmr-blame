"""A two-file project the oracle tests commit into `repo_factory` repositories.

The test command is the cheapest honest one: `check.py`, committed beside the
module it checks, imports `calc` and exits 1 when `total` is wrong. No setup
command, no environment build, no test framework: one interpreter start per
oracle run. The commands here are what an operator would pass as `test`,
so these tests exercise the same path a `terraform test` or `go test` would.

A commit the team must not judge says so itself: `check.py` exits 125, the
`git bisect run` convention the oracle maps to `unbuildable`. A syntax error
in `calc.py` would NOT do: under the generic contract a traceback is a
non-zero exit like any other, and the oracle has no business reading it.
"""

from __future__ import annotations

import shlex
import sys

TEST_COMMAND = f"{shlex.quote(sys.executable)} check.py"

CALC_OK = """\
def total(xs):
    return sum(xs)
"""

# The regression: an off-by-one that only check.py notices.
CALC_BROKEN = """\
def total(xs):
    return sum(xs) + 1
"""

CHECK = """\
import sys

import calc

sys.exit(0 if calc.total([1, 2]) == 3 else 1)
"""

# A commit that cannot be judged: exit 125 is "skip me" to git bisect and
# `unbuildable` to the oracle.
CHECK_SKIP = """\
import sys

sys.exit(125)
"""

# Deterministically noisy: fails on every third invocation, counted through a
# file in the worktree. Interleaved good/bad runs then each see a pass and a
# fail, which is what `flaky` must detect; a coin flip would make the test
# of the detector itself flaky.
CHECK_FLAKY = """\
import sys
from pathlib import Path

counter = Path(".flaky-counter")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
sys.exit(1 if n % 3 == 0 else 0)
"""

# Never finishes within the oracle's timeout.
CHECK_HANG = """\
import time

time.sleep(30)
"""


def project(calc: str = CALC_OK, check: str | None = CHECK) -> dict[str, str]:
    """The files of the first commit; `check=None` ships the module without its test."""
    files = {"calc.py": calc}
    if check is not None:
        files["check.py"] = check
    return files


def touch(n: int) -> dict[str, str]:
    """A commit that changes nothing the test can see."""
    return {"NOTES.md": f"note {n}\n"}
