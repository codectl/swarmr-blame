"""The operator's two commands: how to prepare a checkout, how to run the test.

One responsibility: an operator-supplied command line as an argv list. This is
the whole language interface of the team. The team is git archaeology and does
not know what a test is; the operator does, and says so the way `git bisect
run` has always asked: a command whose exit code is the verdict.

    test="terraform test -filter=nsg_routes.tftest.hcl"
    setup="terraform init -backend=false"

    test="uv run pytest tests/test_cart.py::test_total"
    setup="uv sync"

    test="go test ./... -run TestCheckout"

Split with `shlex`, never run through a shell: the operator may quote, but
nothing the model emits ever reaches these. The model has no tool argument
that names a command; it only chooses refs. Which commands a run uses is
decided in `target.py`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

__all__ = ["Command"]


@dataclass(frozen=True, slots=True)
class Command:
    text: str
    argv: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> Command | None:
        stripped = text.strip()
        if not stripped:
            return None
        argv = tuple(shlex.split(stripped))
        if not argv:
            return None
        return cls(stripped, argv)

    @property
    def paths(self) -> tuple[str, ...]:
        """Arguments that look like repository paths, for absence detection.

        `terraform test -filter=x.tftest.hcl` and `pytest tests/test_x.py::t`
        both name a file the test lives in; when that file is not at a ref the
        test is absent there, not failing. Only the part before `::` or after
        `=` is a path, and only relative ones count.
        """
        found: list[str] = []
        for arg in self.argv[1:]:
            value = arg.split("=", 1)[1] if "=" in arg and arg.startswith("-") else arg
            value = value.split("::", 1)[0]
            if (
                value
                and not value.startswith(("-", "/"))
                and ("." in value or "/" in value)
                and not any(c in value for c in "*?[")
                and value.strip("./") not in ("", "...")
            ):
                found.append(value.removeprefix("./"))
        return tuple(found)
