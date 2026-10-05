# swarmr-blame

Git archaeology team for [swarmr](https://github.com/codectl/swarmr). Given a
command whose exit code says whether a test passes, finds the commit that first
broke it and proves the cause.

Non-mutating to your checkout: every test runs in a `git worktree` under a
scratch directory with a scrubbed environment, and the worktree is removed when
the run ends. Nothing about the repository is hardcoded — the team profiles it
at startup and injects what it found into every prompt.

## Install

Core and this team must land in the **same environment**.

```
uv tool install "swarmr[blame]"
```

That exposes `teams` and `teams-mcp` on your PATH; this team ships no command of
its own. From a checkout: `uv tool install ../swarmr --with ../swarmr-blame`.

```
teams --target repo_forensics --repo /path/to/checkout --test "<command>"
teams repo_forensics --repo /path/to/checkout --test "<command>" --setup "<command>" \
  "the checkout test fails on main since this week"
```

Over MCP the tool is `start_repo_forensics(request, repo, test, setup?)`.

## The oracle

The team does not know what a test is, and does not need to. `test` is run from
the root of a scratch worktree at each commit, the way `git bisect run` has
always asked for one: exit 0 is a pass, anything else is a fail. `setup`, when
given, runs once per worktree and again whenever a lockfile changes between
refs. Any language, any runner — a script you write that checks the symptom is
a test.

The oracle has five answers, and they are distinct on purpose. `pass` is exit 0
and `fail` anything else. `absent` means a file the command names does not
exist at that commit, which is never evidence of breakage. `unbuildable` means
setup failed, the command is not on PATH, or it exited 125 — `git bisect run`'s
"cannot judge", which a wrapper script can use to skip a commit. `timeout` means
the run exceeded `BLAME_ORACLE_TIMEOUT`.

Only commits are tested; an uncommitted edit has no commit to blame and is
reported as a dirty working tree. If the command cannot run at all the report
says `unrunnable`, with the tool's own output, instead of inventing a verdict
about history. An MCP server inherits the environment of whatever launched it,
so the toolchain `test` needs must be on *its* PATH.

## Arguments

Per call, as `--flags` on the CLI or arguments of the MCP tool. Nothing about
the target comes from the environment: a process serving several callers cannot
have one repository.

`repo` is the checkout to investigate — required, must be the repository root,
never modified. `test` is the command whose exit code is the verdict — required,
split with `shlex`, never a shell. `setup` is optional and runs once per
worktree and after every lockfile change.

Tuning: `BLAME_PASS_ENV` (comma-separated variables passed through to the
commands; credentials under `$HOME` are otherwise invisible), `BLAME_ORACLE_TIMEOUT`
(seconds per test run, default 120), `BLAME_INSTALL_TIMEOUT` (seconds per setup
run, default 300), `BLAME_CACHE_TTL` (history reads memoised, default 300; oracle
runs are never cached). Model credentials come from `swarmr` (`KIMI_*`).

There is no network isolation: a test that reaches the network can. Running the
team inside a container is the stronger boundary; this package does not provide
one. The report names a location, never a fix.

## References

- [Architecture overview](./CLAUDE.md)
