"""Where repository code is allowed to run: scratch worktrees and their setup.

One responsibility: the jail. The oracle executes the repository's own code at
arbitrary commits, and no credential can make that read-only, so the boundary
is placement instead: every checkout the team makes is a `git worktree` under a
scratch directory, the setup command runs there, and the process environment
handed to it is scrubbed to a known set.

What this guarantees: the user's checkout is never modified, never has its
HEAD moved, never gains build output, and is never mid-bisect after a crash.
Bisect state lives in `.git/worktrees/<name>/`, per worktree, and the worktree
is removed on exit.

What this does NOT guarantee: network isolation. A test that reaches the
network can. Running the oracle inside a container is the stronger boundary
and is not what this module does; the README says so.

Setup is the operator's `setup` command (`terraform init -backend=false`,
`uv sync`, `npm ci`). It runs once per worktree and again only when a lockfile
changes, so a bisect that crosses a dependency bump prepares exactly once per
side of the bump, and a bisect that does not never prepares again.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Self

from swarmr_blame.command import Command
from swarmr_blame.lockfiles import LOCKFILE_NAMES
from swarmr_blame.repo import git, resolve
from swarmr_blame.target import current

__all__ = [
    "INSTALL_TIMEOUT",
    "SetupError",
    "Worktree",
    "lease",
    "scratch_root",
    "scrubbed_env",
]

INSTALL_TIMEOUT = int(os.environ.get("BLAME_INSTALL_TIMEOUT", "300"))

_MARK = ".swarmr-blame-setup"


class SetupError(RuntimeError):
    """The setup command failed for a ref; carries its output tail."""


@cache
def scratch_root() -> Path:
    """One scratch directory per process, outside the target checkout."""
    root = Path(tempfile.mkdtemp(prefix="swarmr-blame-"))
    (root / "home").mkdir()
    (root / "cache").mkdir()
    return root


# Version managers and package caches live under $HOME, so a scrubbed HOME
# turns every tool behind a shim into "version could not be resolved" and
# makes every run download its dependencies again. Each tool has a variable
# naming its root; set it to the real location when the operator has not, and
# only when that location exists. These are tool roots and caches, not
# credentials. Anything else goes through BLAME_PASS_ENV. Data, not code: a new
# toolchain is a new row.
_TOOLCHAIN_ROOTS = {
    "TFENV_CONFIG_DIR": ".config/tfenv",
    "TF_PLUGIN_CACHE_DIR": ".terraform.d/plugin-cache",
    "ASDF_DIR": ".asdf",
    "ASDF_DATA_DIR": ".asdf",
    "MISE_CONFIG_DIR": ".config/mise",
    "MISE_DATA_DIR": ".local/share/mise",
    "PYENV_ROOT": ".pyenv",
    "NVM_DIR": ".nvm",
    "GOPATH": "go",
    "CARGO_HOME": ".cargo",
    "RUSTUP_HOME": ".rustup",
    "UV_CACHE_DIR": ".cache/uv",
    "npm_config_cache": ".npm",
}

# Caches that fall back to one directory per process, shared by every worktree,
# when the operator has none: a dependency then downloads once per run, not
# once per ref. Only applied when the variable was not already resolved above.
_SCRATCH_CACHES = {
    "TF_PLUGIN_CACHE_DIR": "terraform",
    "npm_config_cache": "npm",
}


def scrubbed_env(worktree: Path) -> dict[str, str]:
    """The process environment the setup and test commands run under.

    Not the operator's: HOME points into scratch so nothing the test writes to
    a dotfile lands in the real one, and no credential-shaped variable is
    inherited unless BLAME_PASS_ENV names it. PATH is kept, because the
    toolchain is on it, and the version managers on it are told where their
    real roots are. Tool caches are pointed into scratch so a provider or wheel
    downloads once per process, not once per ref.
    """
    keep = ["PATH", "LANG", "TZ", "TMPDIR", "SYSTEMROOT", "TEMP", "TMP"]
    keep += list(_TOOLCHAIN_ROOTS)
    keep += [k for k in os.environ.get("BLAME_PASS_ENV", "").split(",") if k]
    env = {key: os.environ[key] for key in keep if key in os.environ}
    real_home = Path(os.environ.get("HOME", "~")).expanduser()
    for var, relative in _TOOLCHAIN_ROOTS.items():
        if var not in env and (real_home / relative).is_dir():
            env[var] = str(real_home / relative)
    scratch = scratch_root()
    for var, name in _SCRATCH_CACHES.items():
        if var not in env:
            (scratch / "cache" / name).mkdir(parents=True, exist_ok=True)
            env[var] = str(scratch / "cache" / name)
    env.update(
        {
            "HOME": str(scratch / "home"),
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "SWARMR_BLAME_SANDBOX": "1",
            # Non-interactive flags, harmless when the tool is absent.
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "CHECKPOINT_DISABLE": "1",
            "UV_PROJECT_ENVIRONMENT": str(worktree / ".venv"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GOFLAGS": "-mod=mod",
        }
    )
    return env


def _setup_key(worktree: Path) -> str:
    """What, when it changes, means setup must run again: every lockfile's bytes."""
    digest = hashlib.sha256()
    for path in sorted(p for p in worktree.rglob("*") if p.name in LOCKFILE_NAMES):
        if ".git" in path.parts or any(part.startswith(".") for part in path.parts[:-1]):
            continue
        digest.update(str(path.relative_to(worktree)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(slots=True)
class Worktree:
    """A disposable checkout under scratch, prepared for whatever is checked out.

    `checkout` moves it between refs; `prepare` runs the setup command when the
    lockfiles changed since it last ran here. Hold `lock` for the span of a
    checkout-and-run, because two callers moving one worktree would each test
    the other's ref. Use as a context manager, or through `lease`, so the
    worktree is removed even when the run aborts.
    """

    name: str
    path: Path
    repo: Path
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(cls, name: str, ref: str = "HEAD") -> Worktree:
        repo = current().repo
        path = scratch_root() / "worktrees" / _repo_key(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            git(["worktree", "prune"], cwd=repo, check=False)
        git(["worktree", "add", "--detach", str(path), ref], cwd=repo)
        return cls(name, path, repo)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.remove()

    def remove(self) -> None:
        # Against the remembered repo, not the active target: removal also
        # runs at exit, when no run is active.
        git(["worktree", "remove", "--force", str(self.path)], cwd=self.repo, check=False)
        shutil.rmtree(self.path, ignore_errors=True)
        git(["worktree", "prune"], cwd=self.repo, check=False)

    def checkout(self, ref: str) -> str:
        """Move this worktree to `ref`; returns the resolved sha.

        The ref is resolved against the TARGET checkout, never this worktree:
        a worktree is detached, so inside it "HEAD" and "HEAD~3" mean "where
        this worktree happens to be", which after one checkout is not what
        the caller meant. Branch names and tags are shared and resolve the
        same either way; only HEAD-relative refs differ, and they are exactly
        what a model writes.
        """
        sha = resolve(ref)
        if sha != self.head():
            git(["checkout", "--detach", "--quiet", sha], cwd=self.path)
        return sha

    def head(self) -> str:
        return git(["rev-parse", "HEAD"], cwd=self.path).strip()

    def prepare(self) -> None:
        """Run the setup command if the lockfiles changed since it last ran here."""
        setup = current().setup
        if setup is None:
            return
        wanted = f"{setup.text}\n{_setup_key(self.path)}"
        mark = self.path / _MARK
        if mark.exists() and mark.read_text() == wanted:
            return
        _run_setup(self.path, setup)
        mark.write_text(wanted)


def _run_setup(worktree: Path, setup: Command) -> None:
    try:
        done = subprocess.run(
            setup.argv,
            cwd=worktree,
            env=scrubbed_env(worktree),
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SetupError(f"setup: `{setup.argv[0]}` is not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(
            f"setup `{setup.text}` exceeded {INSTALL_TIMEOUT}s (BLAME_INSTALL_TIMEOUT)"
        ) from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-15:]
        raise SetupError(
            f"setup `{setup.text}` exited {done.returncode}:\n" + "\n".join(tail)
        )


_leases: dict[tuple[str, str], Worktree] = {}
_lease_lock = threading.Lock()


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo).encode()).hexdigest()[:12]


def lease(name: str) -> Worktree:
    """One persistent worktree per (repository, name) for this process.

    A tool cannot know which investigator called it, so worktrees are leased by
    tool rather than by role: `walk_back`, `bisect`, `compare_refs` and
    `run_oracle` each keep their own, which keeps the setup they ran across
    calls. Two concurrent calls to one tool serialise on the worktree's lock.
    Keyed by repository as well, so two runs in one process pointed at two
    checkouts never share a worktree.
    """
    key = (str(current().repo), name)
    with _lease_lock:
        if key not in _leases:
            if not _leases:
                atexit.register(_release_all)
            _leases[key] = Worktree.create(name)
        return _leases[key]


def _release_all() -> None:
    for worktree in list(_leases.values()):
        worktree.remove()
    _leases.clear()
    shutil.rmtree(scratch_root(), ignore_errors=True)
    # The root is gone; a later run in this process must not be handed its path.
    scratch_root.cache_clear()
