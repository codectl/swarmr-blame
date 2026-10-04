# CLAUDE.md — swarmr-blame

## Overview

Git archaeology team for [swarmr](https://github.com/codectl/swarmr). Given a command whose exit code says whether a test passes — supplied the way `git bisect run` has always asked for one — finds the commit that first broke it and proves the cause. A commander delegates to four investigators in a fixed order, because each step's input is the previous step's output, then a critic independently reruns the test and tries to disprove the hypothesis before it is reported.

Non-mutating to the user's checkout, not read-only: the oracle executes the operator's test command at arbitrary commits. The boundary is placement — every checkout is a `git worktree` under a scratch directory with a scrubbed environment — not a credential. There is no network isolation; the README says so. Nothing about the target repository is hardcoded — `discovery.py` profiles the checkout at build time and the profile is injected into every prompt.

- **Distribution:** `swarmr-blame` (`src/swarmr_blame`, hatchling)
- **Python:** >=3.13; `git` on PATH, plus whatever toolchain the configured commands need
- **Deps:** `swarmr>=1.0,<2` only
- **Entry point:** `swarmr.teams` group → `repo_forensics = "swarmr_blame:TEAM"`
- **Script:** none

**The roster.** `commander` orchestrates and holds no repository tools of its own. `flake` asks whether the test fails at HEAD and passes reliably somewhere before; `bisect` which commit first fails it; `deps` whether a dependency moved in the range; `blame` which hunk in that commit, and what the message claimed versus what the diff did. `critic` reruns the oracle itself and tries to disprove whatever those four produce.

## Entry Points

### The team (`__init__.py` → `TEAM`)

Reached through `swarmr`'s surfaces, not its own:

```
teams --target repo_forensics --repo /path/to/checkout --test "<command>"
teams repo_forensics --repo /path/to/checkout --test "<command>" --setup "<command>" "the symptom, as prose"
```

Over MCP the tool is `start_repo_forensics(request, repo, test, setup?)`.

Importing this module must stay cheap: it is what `teams --list` and the MCP server read to publish the team. `build`, `profile` and `render_report` are declared with `Lazy`; only `digest`, `is_error` and `target.PARAMS` are imported directly.

**Per-call arguments** (`target.PARAMS`, declared on the `Team` as `params`, exposed by core as tool arguments and `--flags`): `repo` (required; checkout root), `test` (required; exit 0 = pass), `setup` (optional; once per worktree and after every lockfile change). **Nothing about the target comes from the environment.** A process serving several MCP callers cannot have one repository, and an investigation reported against the wrong checkout is worse than one that refuses to start.

**Environment (tuning only):** `BLAME_PASS_ENV` (comma-separated variables passed through to the commands), `BLAME_ORACLE_TIMEOUT` (per test run, default 120), `BLAME_INSTALL_TIMEOUT` (per setup run, default 300), `BLAME_CACHE_TTL` (history read memo, default 300). All four are read at call time (`oracle.oracle_timeout()`, `sandbox.install_timeout()`, `output.cache_ttl()`), so a long-lived server honours a retune without a restart. Model credentials come from `swarmr` (`KIMI_*`).

## Architecture

**Team declaration**\
`__init__.py` — the `Team` object: name, summary, routing description, vocabulary (`orchestrator="commander"`, `audit_agents=("critic",)`, `report_tool="file_forensics_report"`, `digest`, `is_error`), roster, `params`, and the `Lazy` targets. Deleting this package removes the team completely; discovery is by entry point, so nothing else in the tree refers to it.

**Assembly**\
`agent.py` — `build(run)` and `profile_target(run)`. Both begin with `target.activate(run.params)`. Stock Deep Agents: one `create_deep_agent` commander plus five `SubAgent` specialists reached through the built-in `task` tool. Holds the filesystem permission rule sets.

**Target**\
`target.py` — `PARAMS`, `Target(repo, test, setup)`, `activate(params)` (rejects an empty `repo` before building a path — `Path("")` is the server's cwd — validates the checkout is a repository root, parses the commands, sets the ContextVar), `current()` (`TargetError(TeamError)` outside a run). `repo.py` — `target()` is `current().repo`, `git()` (argv only, pinned colour/pager/locale/`core.quotepath=false` so non-ASCII paths are never C-quoted on diff headers), `resolve(ref)`. `RepoError` subclasses `swarmr`'s `TeamError`.

**Commands**\
`command.py` — `Command(text, argv)` with `.paths`, the arguments that look like repository-relative files, used for `absent` detection: a `/` or a source-file suffix (`_FILE_SUFFIX`) qualifies, `...` package patterns and bare expressions like `-k foo.bar` do not — a false path makes every ref `absent` and the run `unrunnable`. `shlex` split, never a shell. This is the whole language interface of the team: it does not know what a test is, the operator does.

**Jail**\
`sandbox.py` — `Worktree` (create, `checkout`, `prepare()`), `lease(name)` (one persistent worktree per *(repository, tool)* under `scratch/worktrees/<repo-hash>/<name>`, removed at exit), `scrubbed_env()`, `SetupError`. `_TOOLCHAIN_ROOTS` is a table of *variable → path under the real HOME*, passed through only when that path exists; a new toolchain is a new row, never a function. `_SCRATCH_CACHES` gives package caches a per-process fallback so a dependency downloads once per run.

**Oracle**\
`oracle.py` — `run(worktree, ref, timeout=...) -> Outcome(sha, status, seconds, tail)`. Five statuses, distinct on purpose: `pass`, `fail`, `absent`, `unbuildable`, `timeout`. `Outcome.bisect_code` maps to git's 0/1/125.

**Searches over the oracle**\
`walkback.py` — `walk_back`: HEAD, then `HEAD~1, ~2, ~4, …`, clamped to the root; an `absent` with a path jumps to the commit that added the file. `bisection.py` — `bisect`: verifies both endpoints, drives `git bisect` in-process in the worktree, `reset` in `finally`; a boundary inside skipped commits is a range of candidates, never a sha. `comparison.py` — `compare_refs`: N runs of each ref, interleaved, one worktree; verdict `stable | flaky | inverted | unbuildable | indistinct`.

**History**\
`history.py` — `git_log`, `git_show`, `span()`, `validate_rev`. `hunks.py` — `diff_hunks`, `kept_hunks` and the rename-aware `project()`. `lockfiles.py` — `LOCKFILE_NAMES`, `lockfile_diff` and the parsers for TOML `[[package]]` lockfiles, requirements pins and provider lockfiles; the rest are compared as blobs.

**Repository profiling**\
`discovery.py` — `profile_repo()`, `render_facts()`, `render_mechanics()`. Every call is a read, once at startup, before the first token.

**Prompts**\
`prompts.py` — `commander`, `flake`, `bisect`, `deps`, `blame`, `critic`, the shared `INVESTIGATOR_CONTRACT`, and `SWEEP_REQUEST`. No repository fact appears here; everything target-specific arrives as `facts` and `mechanics`.

**Tools**\
`tools.py` — `run_oracle` plus `FLAKE_TOOLS`, `BISECT_TOOLS`, `DEPS_TOOLS`, `BLAME_TOOLS`, `CRITIC_TOOLS`.

**Result shaping**\
`output.py` — `emit` (byte cap at `MAX_BYTES = 12_000`), `guard` (exception containment), `cached` (TTL memoisation keyed by the active repository as well as the call, expired entries swept on every miss; oracle tools never wear it). `digest.py` — `digest_result`, `is_tool_error`, dispatching on each payload's `kind`; the two fields `core` may call before a run.

**Report**\
`report_tool.py` — `file_forensics_report` tool, `render_report_args`, `commit_author`, `render_hunk`. `redaction.py` — `diagnosis`, `one_line`, `OMITTED_NOTE`, `LOCATION_CAVEAT`.

## Roles and Tool Sets

|Role|Tools|Permissions|
|---|---|---|
|`commander`|`file_forensics_report`|`_NO_WRITES`|
|`flake`|`walk_back`, `compare_refs`, `run_oracle`, `git_log`|`_EVIDENCE_ONLY`|
|`bisect`|`bisect` only — it verifies its own endpoints; anything more was used to re-probe root|`_EVIDENCE_ONLY`|
|`deps`|`lockfile_diff`, `diff_hunks`, `git_log`, `git_show`|`_EVIDENCE_ONLY`|
|`blame`|`git_show`, `diff_hunks`, `git_log`|`_EVIDENCE_ONLY`|
|`critic`|everything|`_NO_WRITES`|

**Only the roles that make a pass/fail claim hold an oracle tool.** `deps` and `blame` read history and can never run code, so a behaviour claim from them is an inference by construction, and their prompts say so. `test_contract.py` pins this.

**The commander holds no repository tools by design:** its context stays clean, and it cannot fabricate an observation it never received. The one tool it holds files the report.

**Permission rule sets are first-match-wins**, and a subagent that omits `permissions` inherits the parent's, so every role states its own. `_EVIDENCE_ONLY` allows `/evidence/**` *before* the catch-all `/**` deny — reversing that order silences the allow and no evidence can be written.

## Flow

```
flake (find bound) → bisect ∥ deps → blame → critic → report
```

Four agent rounds, not six. `deps` needs only the range, so it runs beside `bisect`. There is no second `flake` round to "confirm" the first-bad sha: the critic reruns exactly that parent/sha pair itself, so a confirm round was the same oracle work twice. `compare_refs` defaults to two runs per ref — two interleaved pairs already separate a deterministic break from a flaky test; the ratio only matters once a side comes back `mixed`. Measured on the demo: 27 model calls / 166k tokens and ~20 oracle runs before; the cuts remove two rounds and roughly half the oracle runs.

Unlike the Kubernetes team, round one is **one** investigator: nothing else can start until there is a passing lower bound. The bound is a *passing lower bound*, never "last good" — `walk_back` jumps in powers of two and skips most of history; the boundary is `bisect`'s to find.

`flake`'s findings short-circuit the flow: `never_passed` and `head_passes` go straight to the critic and an honest report; `unrunnable` files the report at once with no critic — the command could not run (unbuildable, timed out or absent at HEAD, or every older commit unbuildable), so there is no hypothesis about the repository to adjudicate, only a sandbox to fix. **`never_passed` requires that history was judged**: a live run once reported it for a test whose toolchain was merely missing from the MCP server's PATH, which is how `unrunnable` was born. "No breaking commit" is a valid outcome.

## Repository Discovery

`profile_repo()` runs before the first token, against the target `activate` has already validated — so a bad `repo` or empty `test` is a `TeamError` before any model is constructed. It measures:

- **Branch and HEAD**, commit count, dirty working tree. The team tests *commits*; an uncommitted edit is invisible to it.
- **Remote** with credentials stripped.
- **Commands** — the test and setup text, which files the test command names and which of those exist at HEAD (`git ls-tree`), whether each command's executable is on PATH.
- **Lockfiles** — every tracked file whose basename is in `LOCKFILE_NAMES`.
- **Tags** — the latest few. A tag is a *hint* for a bound and must be verified by the oracle.
- **CI files** — presence only. CI status is not readable without forge credentials and is never cited.

`render_facts()` emits `<repo>`: the exact test command, the setup command or "none; the checkout is used as committed", a warning when a named file is not at HEAD (the test may be absent there too), lockfiles found ("setup runs again whenever one of these changes between refs") or none ("a dependency drift between refs cannot be excluded"), tool availability.

`render_mechanics()` emits `<oracle-mechanics>`: the five outcomes in command terms, that `absent` is never evidence, that a bound is a lower bound, that `compare_refs` interleaves so an environmental explanation must name a difference that survives interleaving, that bisect may return a range, and that there is no network isolation.

`profile_target()` is separate from `build` because building constructs a model client; `teams --target` must answer "which repository am I pointed at" without an API key.

## Target Resolution

**The target rides a `ContextVar`, not module state.** Tools are module-level functions that take refs, never repositories — the model must not name a path — so they need an ambient channel to the run's target, and a context variable is the one ambient channel that is per task rather than per process. `build` activates it in the thread that streams the graph; LangGraph copies the context into its worker threads, so every tool call sees the target of the run that invoked it. `test_target.py` pins the copy-context propagation.

**`Worktree.checkout` resolves the ref against the target, never the worktree.** A worktree is detached, so inside it `HEAD~3` means "three before wherever this worktree is", which after one checkout is not what anyone meant. Found by smoke, not by reasoning.

**Leases are keyed by repository.** `lease(name)` lives under `scratch/worktrees/<repo-hash>/<name>`, so two runs in one process never share a worktree, and `Worktree` remembers its `repo` so removal at exit needs no active run.

## Tool Design

**No shell, and the command is the operator's.** `test` and `setup` are split with `shlex` and run as argv; the model has no tool argument that names a command or a repository, only refs and counts. `resolve` rejects anything shaped like an option.

**Every tool wears the same wrappers**, `@tool(parse_docstring=True)` → `@guard` → `@cached`, except that **oracle tools are never cached** — a rerun is the point of a rerun. `guard` turns any failure into text the model can act on; an unhandled exception inside a tool aborts the whole LangGraph run. `emit` caps at 12 000 bytes with a message telling the model to narrow the query rather than page.

**Searches are one call.** `walk_back`, `bisect` and `compare_refs` each run the oracle many times internally. The model never drives a search step-by-step with `run_oracle`: it is slower, it mislabels steps, and `git bisect` state would be left behind on a crash.

**Setup runs per worktree, and again only when a lockfile hash changes.** The hash covers *tracked* lockfiles only (`git ls-files` in the worktree): a vendored lockfile under `node_modules/` cannot differ between refs, and hashing it made the key differ before and after the first setup. A bisect across a dependency bump prepares once per side; a bisect that does not cross one never prepares again. With no setup command the checkout is used as committed. With no lockfile the facts say a dependency drift cannot be excluded.

**`scrubbed_env()` passes through PATH, locale, temp, `BLAME_PASS_ENV`, and existing toolchain roots.** HOME points into scratch, so credentials under the real `$HOME` are invisible to the test unless the operator passes their location through.

**`diff_hunks` is rename-aware.** Positional pairing of removed/added lines inside change blocks; a single-token substitution recurring in ≥3 pairs is a rename; a pair is explained when every differing token is a rename, or a compound identifier that embeds one (`with_price` → `with_unit_price`). Hunks made only of explained pairs are dropped and counted; kept hunks mark every unexplained line with `!`. On a rename commit that also changes one value, the projection leaves exactly one `!` line.

**`lockfile_diff` projects, not diffs.** TOML `[[package]]` lockfiles → name/version; requirements → `name==ver` pins, floating requirements as `unpinned`, path and URL requirements keyed by the requirement itself so a vendored bump shows as a removal and an addition; provider lockfiles → provider/version; anything else is reported as moved or not.

## Prompt Design

Shared `INVESTIGATOR_CONTRACT`: scratch worktrees, never the checkout; `grep`/`glob`/`ls` operate on the agent's own scratch filesystem, not the repository — use `git_show`/`diff_hunks`; about 8 calls; counts verbatim; `absent` is neither pass nor fail; `VERDICT / EVIDENCE / INFERENCE`, ≤12 lines.

**The commander is prohibited from enumerating what to check.** Its dispatches are one sentence: the symptom plus the shas the step needs, never a test id and never a checklist. `swarmr`'s `FirstRoundBriefing` enforces symptom-only on `flake` alone (`agent._SHA_DEPENDENT` exempts the rest): unlike the Kubernetes team, every specialist after flake is dispatched exactly once and needs the previous step's output — the bound, the range, the first-bad sha. With the rewrite on, a live run saw bisect re-probe its own endpoints and blame project five commits to find the one it was sent. `test_contract.py` pins the exemption to the roster. `SWEEP_REQUEST`, the default when the caller gives no symptom, is "the configured test fails at HEAD".

**The critic receives the hypothesis alone** and must rerun the oracle at the sha and its parent before anything else; an environmental claim must name a difference that survives interleaving.

**Domain-specific traps encoded in the prompts:**

- `flake` must distinguish `unrunnable` / `never_passed` / `head_passes` / `bound_found` and report the bound as a lower bound; its `VERDICT` is `clean` when the break is deterministic, and the routing finding goes on its first EVIDENCE line. "mixed" on either side is the finding that matters most.
- `bisect` reports a range as a range and never picks a candidate to look decisive.
- `deps`: a lockfile move inside the range is a candidate, not a cause, until it is the first-bad commit or the oracle agrees. Strongest shape: first-bad changes the lockfile and the code filter returns no hunks.
- `blame` compares what the message *claims* with what the diff *does*, cites the hunk header and lines, and says first and prominently if the test file itself changed in the first-bad commit.

**"No breaking commit" is a valid outcome.** `head_passes` and `never_passed` are reported honestly; inventing a first-bad sha to look useful is prohibited.

## Report Filing

The commander's last action is one `file_forensics_report` call. Its arguments **are** the report: `symptom`, `first_bad`, `cause`, `hunk_path`, `evidence`, `critic_ruling`, `dismissed`. `symptom` is which test and how it fails.

Two sections are **measured on the render path, never filed**: the author (`commit_author`, `git log -1 --format='%an <%ae>'` for the sha in `first_bad`) and the HUNK (`render_hunk` → `hunks.kept_hunks`, the projected hunks of `hunk_path` at that sha, `!` markers intact). Both came from live runs where the commander filed a placeholder author and paraphrased a diff into prose — it holds no repo tools, so anything repository-shaped it writes is hearsay. The whole commit is projected before filtering to the path, so renames are still learned from their recurrence.

## Redaction

This team runs the test; it does not write the fix. `cause` goes through `redaction.diagnosis`: any sentence matching a counterfactual (`instead of`, `rather than`, `should be`, `expected`, …) is dropped whole, and `OMITTED_NOTE` labels the gap rather than silently shortening the finding. Sentence granularity is deliberate — clipping a clause leaves mangled punctuation or a prescription with its verb removed. `LOCATION_CAVEAT` accompanies every report: location only, decide and validate the change yourself.

## Design Patterns

**One responsibility per module**, stated in each module docstring: `target` names the run's repository, `command` parses, `sandbox` places, `oracle` judges, `walkback`/`bisection`/`comparison` search, `history`/`hunks`/`lockfiles` read, `output` shapes, `redaction` suppresses, `prompts` is text only.

**Measured, never assumed** — every repository fact in a prompt comes from a live read at build time; the author and the hunk in the report come from git on the render path, never from the model.

**Lazy heavyweight fields** — `build`, `profile` and `render_report` behind `Lazy`, so publishing the MCP surface imports no Deep Agents machinery.

**Placement as the boundary** — scratch worktrees, scrubbed environment, per-repository leases, `git bisect` state reset in `finally`. Not a credential, and not network isolation.

**Fail closed** — no target from the environment, no repository that is not a checkout root, no option-shaped refs, `absent` never counted as a verdict.

**Five outcomes, not two** — `absent`, `unbuildable` and `timeout` are distinct so that "cannot judge" never becomes "broken".

**Searches are tools** — the model asks one question and the composer drives the oracle; step-by-step probing is not a tool shape the model can reach.

**Policy in code, not prose** — redaction as regexes on the render path, the author and hunk measured rather than filed. Both were things a prompt could not hold.

**Errors as feedback** — a tool failure returns text the model can act on; only a target failure is fatal, and it is a `TeamError` so both surfaces print one sentence.

## Testing

No model calls, no package manager. `testpaths = ["src"]`, tests beside the code they cover, excluded from the wheel. `conftest.py` provides `repo_factory(commits, test=DEFAULT_TEST, setup=None)` (builds a repository from commits and activates it as the target) and `point(repo, test, setup)` to re-activate, and clears the tool cache between tests. The cheapest honest oracle is a script committed in the fixture repository (`test="python3 check.py"`) that exits 1 on the regression, 125 for an unbuildable commit, or carries a syntax error to be unbuildable for real; sub-second per run.

- `test_contract.py` — the important one. Pins the vocabulary `core` reads off this team, the oracle-tool restriction per role, that the permission rule sets grant and deny in the right order, that `build`/`profile`/`render_report` stay behind `Lazy`, and — in a subprocess — the import count of publishing the team.
- `test_target.py` — checkout validation, per-context isolation, copy-context propagation to threads, leases keyed by repository.
- `test_walkback.py`, `test_bisection.py`, `test_comparison.py`, `test_oracle.py` — drive the composers against real worktrees with a committed `check.py` oracle.
- `test_command.py`, `test_hunks.py`, `test_lockfiles.py`, `test_history.py`, `test_discovery.py`, `test_report.py`, `test_digest.py` — pure functions over fixtures.

```
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ../swarmr -e ".[dev]"
pytest
ruff check . && pyright
```

This project pins `venvPath="."`/`venv=".venv"`: that is the only project-level signal an editor's pyright reads, and without it the language server type-checks against whatever bare interpreter is on PATH. CI therefore builds `.venv` too, so the one path is correct everywhere.

CI (`.github/workflows/ci.yml`) uses `uv`, installs `swarmr` from git `@main` **before** the editable install, sets a git identity for the fixture repositories, then runs `pytest`, `ruff check .` and `pyright` from `.venv/bin/python`. Releases (`.github/workflows/publish.yml`): release-please opens the release PR on every push to `main`; merging it tags, creates the GitHub Release and publishes to PyPI through trusted publishing in the same workflow run.

## Dependencies

- `swarmr>=1.3,<2` — capped to a minor because `Team`, `Member`, `Param`, `RunContext`, `TeamBuild`, `Lazy`, `TeamError`, `core.middleware` and the `swarmr.teams` group are an ABI this package implements; `1.3` is where `Param` and `profile(run)` arrived. From it: `build_model` (never a model constructed here), `AnnounceName`, `FirstRoundBriefing`, `Attribution`, `clip`.
- `deepagents` / `langchain-core` — reached transitively for `create_deep_agent`, `SubAgent`, `FilesystemPermission` and the `@tool` decorator.
- `git` on PATH — every repository read and every worktree; no Python git binding.
- Standard library only otherwise: `shlex`, `subprocess`, `contextvars`, `tomllib`, `hashlib`.
- Dev: `pytest`, `pytest-cov`, `ruff` (line length 90; `E,F,I,UP,B,SIM,RUF`), `pyright` (standard mode, 3.13).
