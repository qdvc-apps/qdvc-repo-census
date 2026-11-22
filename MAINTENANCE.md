# Maintenance notes

## Layout

Everything lives in `repocensus.py`, a single standard-library module, so it can be copied onto any machine and run. Sections, in file order:

| Section | Key names | Notes |
|---|---|---|
| Running git | `run_git`, `GIT_ENV`, `kill_all_active` | Every git call goes through `run_git`, which never raises for git errors. Each call runs in its own process group so a timeout or Ctrl-C can kill `ssh` too. |
| Finding repositories | `find_repos`, `is_repo_dir` | `.git` may be a file (worktrees, submodules). Doesn't descend into repos, hidden folders or symlinks. |
| Remote URLs and servers | `parse_remote_url`, `Endpoint`, `resolve_ssh_alias`, `Server`, `check_homepage`, `check_server`, `proxy_for`, `no_proxy_matches` | Pure parsing is unit-tested. `check_server` runs in a daemon thread with a hard deadline because urllib's timeout doesn't cover DNS. |
| Per-repo inspection | `Census.process` -> `Census.fetch_remotes` -> `inspect_repo` | One worker per repo. Remotes within a repo are fetched one after another to avoid ref-lock and `FETCH_HEAD` collisions. `Census.lock` guards the shared `Server` circuit-breaker state. |
| Output | `render`, `Style`, `Progress`, `want_color` | Output is ASCII-only, so Windows consoles with legacy code pages can't raise `UnicodeEncodeError`. Progress goes to stderr, and only when it's a terminal. |
| Main | `build_parser`, `plan_servers`, `main` | Phases: read remotes (local only), check servers (once each, in parallel), fetch and inspect (parallel per repo), render. |

## Report items

`RepoReport.items` holds `Item(level, text)` entries:

* `issue`: needs attention. Makes the exit status 1.
* `stale`: a remote couldn't be fetched. Makes the exit status 3 if there are no issues.
* `info`: context only.

`RemoteInfo.fetch_state` is one of `fetched`, `unchecked` (fetched, but its transport couldn't be connectivity-checked), `skipped`, `failed` or `disabled` (`--no-fetch`). Keep these names exact. Don't reuse a field to mean two things.

## Conventions

* Never add a git command that changes the working tree, the index, local branches or the remotes. `git fetch` is the only command allowed to talk to a server, always with `FETCH_CONFIG` and `FETCH_ARGS`.
* Proxy decisions go through `proxy_for()` only, which mirrors curl/git. Never call `urllib.request.getproxies()` or `proxy_bypass()`: on macOS and Windows they read OS proxy settings that git ignores. Always pass an explicit `ProxyHandler`.
* git output is parsed with `LC_ALL=C` and NUL-separated formats (`-z`, `%00`) wherever paths or names could contain odd characters.
* Don't use `strftime("%-d")` or similar glibc-only codes. They fail on Windows.
* Tests must not depend on the developer's git config. `setUpModule` points `HOME` and `GIT_CONFIG_GLOBAL` at a temporary folder and sets `GIT_CONFIG_NOSYSTEM=1`. Proxy variables, including `no_proxy`, are cleared so homepage tests are deterministic. A leftover `no_proxy` containing `127.0.0.1` once made a proxy regression test pass against the buggy code.

## Release checklist

1. Bump `__version__` in `repocensus.py` and `version` in `pyproject.toml`.
2. Run `python3 -m unittest discover -s tests`.
3. Do a smoke test on a real folder, with one remote deliberately unreachable.
