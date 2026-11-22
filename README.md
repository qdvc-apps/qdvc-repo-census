# qdvc-repo-census

Take a census of every Git repo in a folder: what's uncommitted, what's unpushed, what's waiting to be pulled from each remote, which remote each repo tracks, and anything that breaks the "one branch, `main`, everywhere" rule.

Part of the [QDVC suite](https://github.com/qdvc-apps).

It's built for clones that track several remotes (say `alpha` and `bravo`), where one server sometimes can't be reached. Before fetching anything, it gathers every remote across all the repos, groups them by server, and checks each server's homepage once. It doesn't fetch from servers that don't answer, so an offline server is contacted once per run rather than once per repo. For those remotes the report shows the last-known state instead.

It runs `git fetch` and nothing else that talks to a server. It never pulls, pushes, merges, prunes or garbage-collects.

## Usage

```
cd ~/projects/client-x       # a folder full of clones
repocensus                   # or: python3 /path/to/repocensus.py
```

Example output:

```
qdvc-repo-census 0.1.1 - /home/me/projects/client-x (4 repos, depth 1)

Servers
  [ok] git.alpha.example    online   remotes: alpha; 4 repos
         https://git.alpha.example/ -> HTTP 302 (direct)
  [--] site-office.lan      OFFLINE  remotes: bravo; 4 repos
         https://site-office.lan/: timed out (direct); http://site-office.lan/: timed out (direct)
         site-office.lan resolves to 192.168.40.12
         not fetched from; last-known state shown instead

Repositories
  [!!] docs        tracks alpha/main (also: bravo)
       - alpha: 1 commit to pull
       - alpha/main and bravo/main differ: alpha has 1 commit not on bravo
  [??] site-tools  tracks bravo/main (also: alpha)
       ? bravo: not fetched, server offline (homepage did not answer)
         bravo: tracking ref last changed 2026-11-10 14:02, 12 days ago
  [ok] widget-api  tracks alpha/main (also: bravo)  all clean and in sync
  [!!] widget-ui   tracks alpha/main (also: bravo)
       - uncommitted changes: 1 modified, 1 untracked
       - 1 commit on 'main' not on any remote
       - alpha: 1 commit to push

Summary: 4 repos, 1 clean, 2 need attention, 1 clean as far as known (some remotes not fetched), 1 server offline or failing.
```

`[ok]` means clean and in sync. `[!!]` means something needs attention. `[??]` means nothing was flagged, but at least one remote couldn't be fetched, so the picture may be out of date.

### What is reported

For each repo:

* **Uncommitted work:** staged, modified, untracked and conflicted files, plus saved stashes.
* **Unpushed commits:** commits on `main` that aren't on *any* remote, then per remote "N to push". Commits on other local branches are counted too.
* **Unpulled commits:** per remote, "N to pull".
* **Tracking:** which remote (and branch) `main` tracks, and the other remotes. It's flagged if `main` has no upstream, tracks a local branch, tracks a remote that doesn't exist, or tracks a branch not called `main`.
* **One-branch rule:** flags any local branch other than `main`, any remote branch other than `main`, a missing `main` (locally or on a remote), and a checkout on something other than `main`.
* **Remotes disagreeing:** when `alpha/main` and `bravo/main` point at different commits, it says which has commits the other lacks.
* **Repo state:** detached HEAD, and a merge, rebase, cherry-pick, revert, am or bisect in progress.
* **Fetch problems:** servers that are offline, local-path remotes that have vanished, and fetches that failed, with a hint for authentication problems.

### Options

| Option | Meaning |
|---|---|
| `folder` | Folder to scan (default: current folder). If it is itself a repo, just that repo is reported. |
| `-r`, `--recursive` | Find repos at any depth, not just immediate subfolders. Hidden folders and `node_modules` are not descended into. |
| `-d N`, `--depth N` | Search N levels deep (default 1, or unlimited with `-r`). |
| `-j N`, `--jobs N` | Repos fetched and inspected in parallel (default 4). Remotes within one repo are always fetched one at a time. |
| `-n`, `--no-fetch` | Don't contact any server. Report from what was fetched last time. |
| `-b NAME`, `--branch NAME` | The one branch everything should have (default `main`). |
| `-e LIST`, `--expect-remotes LIST` | Flag repos missing any of these remotes, e.g. `-e alpha,bravo`. |
| `-q`, `--quiet` | Hide repos that are all clean. |
| `--timeout S` | Seconds to wait for each homepage (default 5). |
| `--fetch-timeout S` | Seconds before a single `git fetch` is abandoned (default 120). |
| `--color auto\|always\|never` | Colour output. `NO_COLOR` and `FORCE_COLOR` are honoured, so `FORCE_COLOR=1 repocensus \| less -R` keeps colour. |

### Exit status

| Code | Meaning |
|---|---|
| 0 | Everything clean and in sync, and every server reached |
| 1 | At least one repo needs attention |
| 2 | Usage error, or no repos found |
| 3 | Nothing flagged, but some remotes couldn't be fetched |
| 130 | Interrupted (Ctrl-C) |

## How the server check works

1. Every remote URL in every repo is read with `git remote get-url`, so `insteadOf` rewrites are applied.
2. The server host is taken from the URL (`https://`, `ssh://`, `git://` and `user@host:path` forms are all understood). For SSH remotes, host aliases from `~/.ssh/config` are resolved to the real host name with `ssh -G`, which doesn't connect.
3. Each distinct host is checked once, all at the same time: `https://host/`, then `http://host/`. HTTP(S) remotes on a non-standard port try that port first. **Any** HTTP reply counts as online, including 404, 401 and redirects. A TLS handshake also counts, even with a self-signed or mismatched certificate, because only a `HEAD` request is sent and nothing else.
4. Remotes on hosts that didn't answer are skipped. If a fetch to an online host then fails with a network error, the host is marked as failing and later fetches to it are skipped as well.
5. Remotes that are local paths are checked by seeing whether the path exists.

Proxies: the check makes the same proxy decision git makes over HTTPS (via curl), so the two can't disagree about a host:

* Only environment variables are used: `http_proxy` (lower case only, as in curl), `https_proxy`/`HTTPS_PROXY`, then `all_proxy`/`ALL_PROXY`.
* The proxy in **macOS System Settings** or **Windows Internet Options** is ignored, because git ignores it too. Python's urllib would otherwise use it whenever no variable is set. That sent LAN hosts such as `https://charlie9:3030/` to a proxy that couldn't reach them, so they showed as offline on a Mac but not on Linux (fixed in 0.1.1).
* `no_proxy` is matched the way curl matches it, including CIDR entries such as `10.0.0.0/8`, which Python's own matching ignores.
* When a check goes via a proxy, a 502, 503 or 504 counts as offline, because that's the proxy saying it couldn't reach the server.
* A proxy set in git's own config (`http.proxy`) isn't used by the check.

Each server line says whether the check went `direct` or `via proxy …`. For an offline server it also says what the host name resolves to.

### Limits of the homepage check

* If a server runs SSH but no web server, it will always look offline, and its remotes won't be fetched. The same goes for a server reached through an SSH `ProxyJump` host. Use `--no-fetch` to see the cached state. A fallback that tries the git port (e.g. TCP 22) when the homepage doesn't answer would be a small addition.
* A homepage that answers doesn't guarantee that git access works. Fetch failures are reported separately.
* On macOS 15 and later, Local Network privacy can block a program from reaching machines on your network, typically with "no route to host". If a LAN server shows that error, the report suggests allowing your terminal app under System Settings > Privacy & Security > Local Network.

## Safety

* Only `git fetch` is run against servers, with automatic `gc`/maintenance switched off and submodule recursion disabled. Your own `fetch.prune` setting, if you have one, still applies.
* `git status` runs with `GIT_OPTIONAL_LOCKS=0`, so the tool never takes `index.lock` and can't collide with a git command you're running in another terminal.
* Fetches never wait for input. `GIT_TERMINAL_PROMPT=0` stops HTTPS password prompts. Unless you've set `GIT_SSH_COMMAND`, `GIT_SSH` or `core.sshCommand` yourself, SSH runs with `BatchMode=yes`, so a passphrase-protected key that isn't in your agent, or an unknown host key, fails fast instead of hanging. Connect to a new SSH host once by hand to accept its key.
* Ctrl-C stops all running fetches, including their `ssh` child processes.

## Install

Requires Python 3.9+ and git 2.13+. It uses no third-party packages.

* Run it directly with `python3 repocensus.py`, or copy it onto your `PATH` as `repocensus` and make it executable.
* Or install it as a command with `pipx install .` (or `pip install --user .`) from a checkout.

## Development

```
python3 -m unittest discover -s tests
```

The tests build real repositories with on-disk "servers" in a temporary folder. They isolate git's global and system config, so your own settings can't affect the results. See [MAINTENANCE.md](MAINTENANCE.md) for the code layout.

Platform gotchas found while building this go in [`cursed-knowledge-reports/`](cursed-knowledge-reports/), following the [QDVC Cursed Knowledge](https://github.com/qdvc-apps/qdvc-cursed-knowledge) guidelines.

## Acknowledgements

Written with assistance from Claude (Anthropic).
