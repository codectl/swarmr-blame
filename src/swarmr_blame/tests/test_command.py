"""command: an operator command line as argv, and which arguments name files."""

from __future__ import annotations

import pytest

from swarmr_blame.command import Command


def test_parse_splits_like_a_shell_without_being_one() -> None:
    command = Command.parse('  terraform test -filter="nsg routes.tftest.hcl" ')
    assert command is not None
    assert command.text == 'terraform test -filter="nsg routes.tftest.hcl"'
    assert command.argv == ("terraform", "test", "-filter=nsg routes.tftest.hcl")
    assert Command.parse("   ") is None


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("terraform test -filter=x.tftest.hcl", ("x.tftest.hcl",)),
        ("uv run pytest tests/test_x.py::test_y", ("tests/test_x.py",)),
        ("pytest ./tests/test_x.py -k total", ("tests/test_x.py",)),
        ("pytest -x --tb=short tests/test_x.py", ("tests/test_x.py",)),
        ("python3 /usr/bin/check.py", ()),
        ("go test ./... -run TestCheckout", ()),
        ("pytest tests/test_*.py", ()),
        ("make check", ()),
    ],
)
def test_paths_are_relative_file_arguments(text: str, paths: tuple[str, ...]) -> None:
    command = Command.parse(text)
    assert command is not None
    assert command.paths == paths
