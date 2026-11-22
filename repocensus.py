#!/usr/bin/env python3
"""qdvc-repo-census: take a census of every Git repo in a folder.

Run it in a folder that contains Git clones. It reports, per repo:

* uncommitted local changes (staged, unstaged, untracked, conflicts, stashes),
* local commits that are not on any remote, and per-remote ahead/behind counts,
* upstream commits that have not been pulled, for every remote,
* which remote the main branch tracks,
* anything that breaks the "one branch, main, everywhere" assumption.

Before fetching, it gathers every remote across all repos, groups them by
server, and checks each server's homepage once. Servers that do not answer are
not fetched from, and the report shows the last-known state for them instead.

The tool runs ``git fetch`` only. It never pulls, pushes, merges, prunes or
garbage-collects.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

__version__ = "0.1.1"
PROG = "repocensus"

# Exit codes.
EXIT_OK = 0  # everything clean, in sync, and every server reached
EXIT_ATTENTION = 1  # at least one repo needs attention
EXIT_USAGE = 2  # bad arguments, or no repos found
EXIT_INCOMPLETE = 3  # nothing flagged, but some remotes could not be fetched
EXIT_INTERRUPTED = 130

# Environment for every git call.
#   GIT_OPTIONAL_LOCKS=0  stops `git status` taking index.lock, so we never
#                         collide with a git command the user is running.
#   GIT_TERMINAL_PROMPT=0 makes HTTPS fetches fail instead of waiting for a
#                         username/password nobody will type.
#   LC_ALL=C              keeps git's error messages in English, so the
#                         network/auth classification below works everywhere.
GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}

# Config passed to `git fetch` so it does nothing beyond fetching:
# no automatic gc/maintenance (which can prune unreachable objects), and no
# recursion into submodules (whose servers were never connectivity-checked).
FETCH_CONFIG = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")
FETCH_ARGS = ("fetch", "--quiet", "--recurse-submodules=no")

NETWORK_ERROR_PATTERNS = (
    "could not resolve host",
    "could not resolve hostname",
    "name or service not known",
    "temporary failure in name resolution",
    "connection timed out",
    "operation timed out",
    "connection refused",
    "no route to host",
    "network is unreachable",
    "failed to connect to",
    "couldn't connect to server",
    "ssh: connect to host",
    "connection reset by peer",
    "connection closed by remote host",
)
AUTH_ERROR_PATTERNS = (
    "permission denied",
    "authentication failed",
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "host key verification failed",
    "access denied",
    "repository not found",
)


# --------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


_active_procs: Set[subprocess.Popen] = set()
_active_lock = threading.Lock()


def _git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_ENV)
    if extra:
        env.update(extra)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a git process and anything it spawned (ssh, credential helpers)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # Windows
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_git(
    repo: str,
    *args: str,
    timeout: Optional[float] = 60,
    env_extra: Optional[Dict[str, str]] = None,
) -> GitResult:
    """Run git in `repo` and return decoded output. Never raises for git errors."""
    cmd = ["git", "-C", repo, *args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(env_extra),
            # Own process group, so a timeout can kill ssh as well as git.
            start_new_session=hasattr(os, "killpg"),
        )
    except OSError as exc:
        return GitResult(127, "", f"could not run git: {exc}")
    with _active_lock:
        _active_procs.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            timed_out = True
    finally:
        with _active_lock:
            _active_procs.discard(proc)
    return GitResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        timed_out,
    )


def kill_all_active() -> None:
    with _active_lock:
        procs = list(_active_procs)
    for proc in procs:
        _kill_tree(proc)


# --------------------------------------------------------------------------
# Finding repositories
# --------------------------------------------------------------------------

SKIP_DESCEND = {"node_modules", "__pycache__"}


def is_repo_dir(path: str) -> bool:
    # `.git` is a directory in a normal clone and a file in worktrees/submodules.
    return os.path.exists(os.path.join(path, ".git"))


def find_repos(root: str, max_depth: Optional[int]) -> Tuple[List[str], List[str]]:
    """Return (repo paths, warnings). Depth 1 means immediate subfolders only.

    Does not descend into a repo once found (so submodules and vendored clones
    are not reported separately), into hidden folders, or through symlinks.
    A symlink that points straight at a repo is still included.
    """
    root = os.path.abspath(root)
    warnings: List[str] = []
    if is_repo_dir(root):
        return [root], warnings
    found: List[str] = []
    seen_real: Set[str] = set()

    def add(path: str) -> None:
        real = os.path.realpath(path)
        if real not in seen_real:
            seen_real.add(real)
            found.append(path)

    def walk(directory: str, depth: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"could not read {directory}: {exc.strerror or exc}")
            return
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir()  # follows symlinks
            except OSError:
                continue
            if not is_dir:
                continue
            if is_repo_dir(entry.path):
                add(entry.path)
                continue
            if is_link or entry.name.startswith(".") or entry.name in SKIP_DESCEND:
                continue
            if max_depth is None or depth < max_depth:
                walk(entry.path, depth + 1)

    walk(root, 1)
    return found, warnings


# --------------------------------------------------------------------------
# Remote URLs and servers
# --------------------------------------------------------------------------

SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
SSH_SCHEMES = {"ssh", "git+ssh", "ssh+git"}


@dataclass(frozen=True)
class Endpoint:
    """Where a remote URL points, as far as connectivity checking is concerned.

    kind is "network" (has a host to check), "local" (a path on disk) or
    "unknown" (a transport we cannot check, e.g. ext::).
    """

    kind: str
    host: Optional[str] = None  # host as written in the URL (lowercase)
    homepages: Tuple[str, ...] = ()
    local_path: Optional[str] = None
    ssh_style: bool = False


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def default_homepages(host: str) -> Tuple[str, ...]:
    h = _bracket(host)
    return (f"https://{h}/", f"http://{h}/")


def parse_remote_url(url: str, repo_dir: str = ".") -> Endpoint:
    url = url.strip()
    if "::" in url.split("/", 1)[0] and not SCHEME_RE.match(url):
        # Git's <transport>::<address> syntax (ext::, fd::, remote helpers).
        return Endpoint("unknown")

    m = SCHEME_RE.match(url)
    if m:
        scheme = m.group(1).lower()
        parts = urllib.parse.urlsplit(url)
        if scheme == "file":
            path = urllib.parse.unquote(parts.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            return Endpoint("local", local_path=os.path.normpath(path))
        host = (parts.hostname or "").lower()
        if not host:
            return Endpoint("unknown")
        if scheme in ("http", "https"):
            try:
                port = parts.port
            except ValueError:
                port = None
            own = f"{scheme}://{_bracket(host)}{':' + str(port) if port else ''}/"
            pages = [own] + [p for p in default_homepages(host) if p != own]
            return Endpoint("network", host, tuple(pages))
        return Endpoint(
            "network", host, default_homepages(host), ssh_style=scheme in SSH_SCHEMES
        )

    # scp-like syntax: [user@]host:path, recognised by git only when the
    # first colon comes before any slash.
    colon = url.find(":")
    slash = url.find("/")
    if colon > 0 and (slash == -1 or colon < slash):
        hostpart = url[:colon]
        # A single letter before the colon is a Windows drive (C:\repo).
        if not (len(hostpart) == 1 and hostpart.isalpha()):
            host = hostpart.rsplit("@", 1)[-1].strip("[]").lower()
            if host:
                return Endpoint("network", host, default_homepages(host), ssh_style=True)

    path = os.path.expanduser(url)
    if not os.path.isabs(path):
        path = os.path.join(repo_dir, path)
    return Endpoint("local", local_path=os.path.normpath(path))


_ssh_alias_cache: Dict[str, str] = {}
_ssh_alias_lock = threading.Lock()


def resolve_ssh_alias(host: str) -> str:
    """Map an ~/.ssh/config Host alias to its real HostName via `ssh -G`.

    `ssh -G` prints the effective configuration without connecting. If ssh is
    not installed or fails, the host is returned unchanged.
    """
    with _ssh_alias_lock:
        if host in _ssh_alias_cache:
            return _ssh_alias_cache[host]
    resolved = host
    ssh = shutil.which("ssh")
    if ssh:
        try:
            res = subprocess.run(
                [ssh, "-G", host],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )
            if res.returncode == 0:
                for line in res.stdout.decode("utf-8", "replace").splitlines():
                    key, _, value = line.partition(" ")
                    if key.lower() == "hostname" and value.strip():
                        resolved = value.strip().lower()
                        break
        except (OSError, subprocess.SubprocessError):
            pass
    with _ssh_alias_lock:
        _ssh_alias_cache[host] = resolved
    return resolved


@dataclass
class Server:
    host: str
    homepages: List[str] = field(default_factory=list)
    remote_names: Set[str] = field(default_factory=set)
    aliases: Set[str] = field(default_factory=set)
    repo_count: int = 0
    online: Optional[bool] = None  # None = not checked
    detail: str = ""
    notes: List[str] = field(default_factory=list)  # diagnosis when offline
    # Set when a fetch failed with a network error after the homepage check
    # passed; later fetches to this server are skipped.
    tripped: str = ""

    @property
    def reachable(self) -> bool:
        return bool(self.online) and not self.tripped


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is proof enough that the server answered; don't follow it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _no_proxy_entries() -> List[str]:
    raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    return [e.strip() for e in raw.split(",") if e.strip()]


def no_proxy_matches(host: str, entries: Sequence[str]) -> bool:
    """curl-compatible no_proxy matching, including CIDR ranges.

    Python's urllib ignores CIDR entries such as 10.0.0.0/8 (and `*.domain`
    wildcards), while curl, which git uses for HTTPS, honours them. Without
    this, the homepage check could go via a proxy for a host git would reach
    directly, and wrongly call it offline.
    """
    host = host.strip("[]").lower().rstrip(".")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    for entry in entries:
        entry = entry.lower()
        if entry == "*":
            return True
        if addr is not None:
            try:
                if addr in ipaddress.ip_network(entry.strip("[]"), strict=False):
                    return True
                continue
            except ValueError:
                pass
        name = entry
        if name.startswith("*"):
            name = name[1:]
        name = name.lstrip(".")
        if ":" in name and name.count(":") == 1:  # host:port; curl ignores ports
            name = name.split(":", 1)[0]
        if name and (host == name or host.endswith("." + name)):
            return True
    return False


def env_proxy(scheme: str) -> Optional[str]:
    """The proxy curl (and therefore git) would use for `scheme`.

    Environment variables only, read the way curl reads them: `http_proxy` in
    lower case only, `https_proxy`/`HTTPS_PROXY`, then `all_proxy`/`ALL_PROXY`.

    Deliberately NOT urllib.request.getproxies(): on macOS (and Windows) that
    falls back to the proxy in System Settings (or Internet Options) when no
    environment variable is set. git ignores those settings, so using them
    sent the check through a proxy that git never uses; a proxy that can't see
    a LAN host such as `charlie9` then made it look offline.
    """
    if scheme == "http":
        names: Tuple[str, ...] = ("http_proxy",)
    elif scheme == "https":
        names = ("https_proxy", "HTTPS_PROXY")
    else:
        names = ()
    for name in names + ("all_proxy", "ALL_PROXY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value if "://" in value else "http://" + value
    return None


def proxy_for(url: str) -> Optional[str]:
    """The proxy URL git would use for `url`, or None for a direct connection."""
    parts = urllib.parse.urlsplit(url)
    proxy = env_proxy(parts.scheme)
    if not proxy or no_proxy_matches(parts.hostname or "", _no_proxy_entries()):
        return None
    return proxy


def _uses_proxy(url: str) -> bool:
    return proxy_for(url) is not None


def _proxy_label(proxy: str) -> str:
    """Proxy URL for display, without any user:password."""
    parts = urllib.parse.urlsplit(proxy)
    host = parts.hostname or proxy
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{_bracket(host)}{port}"


def check_homepage(url: str, timeout: float) -> Tuple[bool, str]:
    """Return (online, detail). Any HTTP reply at all counts as online.

    Certificates are not verified: a self-signed site-office server should
    count as up, and nothing is sent but a HEAD request. When the request goes
    via an HTTP proxy, a 502/503/504 is the proxy saying it could not reach the
    server, so it counts as offline.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    proxy = proxy_for(url)
    route = "direct"
    if proxy and not proxy.lower().startswith("http://"):
        # urllib can only tunnel through plain-HTTP proxies (not SOCKS etc.).
        route = f"direct; proxy {_proxy_label(proxy)} not usable by this check"
        proxy = None
    elif proxy:
        route = f"via proxy {_proxy_label(proxy)}"
    scheme = urllib.parse.urlsplit(url).scheme
    # An explicit ProxyHandler stops urllib consulting the environment or the
    # OS proxy settings itself: proxy_for() has already decided, like git.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({scheme: proxy} if proxy else {}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": f"qdvc-repo-census/{__version__}"}
    )
    ok, what = _attempt(opener, req, timeout, bool(proxy))
    return ok, f"{what} ({route})"


def _attempt(opener, req, timeout: float, proxied: bool) -> Tuple[bool, str]:
    try:
        with opener.open(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 503, 504) and proxied:
            return False, f"proxy returned HTTP {exc.code}"
        return True, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return True, f"TLS error but server answered ({reason.__class__.__name__})"
        if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
            return True, "server accepted the connection, then closed it"
        return False, _describe_reason(reason)
    except ssl.SSLError as exc:
        return True, f"TLS error but server answered ({exc.__class__.__name__})"
    except (ConnectionResetError, ConnectionAbortedError):
        return True, "server accepted the connection, then closed it"
    except (TimeoutError, OSError) as exc:
        return False, _describe_reason(exc)
    except Exception as exc:  # http.client oddities (BadStatusLine etc.)
        name = exc.__class__.__name__
        if name in ("RemoteDisconnected", "BadStatusLine", "LineTooLong"):
            return True, f"server answered oddly ({name})"
        return False, f"{name}: {exc}"


def _describe_reason(reason: object) -> str:
    if isinstance(reason, socket.gaierror):
        return "host name not found"
    err = getattr(reason, "errno", None)
    if err == errno.EHOSTUNREACH:
        return "no route to host"
    if err == errno.ENETUNREACH:
        return "network unreachable"
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return "timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    text = str(reason)
    if "Name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return "host name not found"
    return text or reason.__class__.__name__


def _resolve_note(host: str) -> str:
    """What the host name resolves to, for the diagnosis of an offline server."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"{host} does not resolve ({exc.strerror or exc})"
    except OSError as exc:
        return f"{host}: lookup failed ({exc})"
    addrs: List[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in addrs:
            addrs.append(addr)
    return f"{host} resolves to {', '.join(addrs)}" if addrs else f"{host} resolves to nothing"


MACOS_LOCAL_NETWORK_HINT = (
    "on macOS, 'no route to host' for a machine on your network can mean Local"
    " Network privacy is blocking this Python: allow your terminal app under"
    " System Settings > Privacy & Security > Local Network, then restart it"
)


def check_server(server: Server, timeout: float) -> None:
    """Try each homepage in turn; the server is online if any answers.

    Runs in a daemon thread with a hard deadline because urllib's timeout does
    not cover DNS lookups, which can stall for a long time on site networks.
    """
    result: Dict[str, object] = {}
    notes: List[str] = []

    def worker() -> None:
        details = []
        for url in server.homepages:
            ok, detail = check_homepage(url, timeout)
            if ok:
                result["online"], result["detail"] = True, f"{url} -> {detail}"
                return
            details.append(f"{url}: {detail}")
        # Offline: record enough to diagnose it without another round trip.
        notes.append(_resolve_note(server.host))
        if sys.platform == "darwin" and any("no route to host" in d for d in details):
            notes.append(MACOS_LOCAL_NETWORK_HINT)
        result["online"], result["detail"] = False, "; ".join(details)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # Generous: Python tries each resolved address in turn with the full
    # timeout (no "happy eyeballs" as in curl), so a dead IPv6 address before
    # a working IPv4 one costs a whole timeout.
    t.join(timeout * len(server.homepages) * 2 + 5)
    server.notes = list(notes)
    if "online" in result:
        server.online = bool(result["online"])
        server.detail = str(result["detail"])
    else:
        server.online = False
        server.detail = "no answer before the deadline (DNS lookup or connection stalled)"


# --------------------------------------------------------------------------
# Per-repo inspection
# --------------------------------------------------------------------------


@dataclass
class StatusCounts:
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicted: int = 0

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked or self.conflicted)


CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def parse_status_z(data: str) -> StatusCounts:
    """Parse `git status --porcelain=v1 -z` output into counts."""
    counts = StatusCounts()
    entries = data.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        x, y = xy[0], xy[1]
        if xy == "??":
            counts.untracked += 1
            continue
        if xy == "!!":
            continue
        if xy in CONFLICT_CODES:
            counts.conflicted += 1
        else:
            if x not in " ?":
                counts.staged += 1
            if y not in " ?":
                counts.unstaged += 1
        if x in "RC" or y in "RC":
            i += 1  # the original path follows as its own NUL-terminated field
    return counts


def parse_config_z(data: str) -> Dict[str, List[str]]:
    """Parse `git config -z --list`. Keys are as git prints them."""
    config: Dict[str, List[str]] = {}
    for item in data.split("\0"):
        if not item:
            continue
        key, _, value = item.partition("\n")
        config.setdefault(key, []).append(value)
    return config


@dataclass
class RemoteInfo:
    name: str
    url: str = ""
    endpoint: Optional[Endpoint] = None
    server_key: Optional[str] = None
    fetch_state: str = "pending"  # fetched | skipped | failed | disabled | unchecked
    fetch_detail: str = ""
    branches: List[str] = field(default_factory=list)  # remote-tracking branch names
    has_main: bool = False
    ahead: Optional[int] = None  # local main commits not on remote main
    behind: Optional[int] = None  # remote main commits not in local main
    ref_changed: Optional[int] = None  # unix time the tracking ref last changed


@dataclass
class Item:
    level: str  # "issue" | "stale" | "info"
    text: str


@dataclass
class RepoReport:
    path: str
    name: str
    remotes: List[RemoteInfo] = field(default_factory=list)
    error: str = ""
    git_dir: str = ""
    head_branch: Optional[str] = None
    detached: bool = False
    unborn: bool = False
    tracking_remote: Optional[str] = None
    tracking_branch: Optional[str] = None
    items: List[Item] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.items.append(Item(level, text))

    @property
    def has_issue(self) -> bool:
        return bool(self.error) or any(i.level == "issue" for i in self.items)

    @property
    def has_stale(self) -> bool:
        return any(i.level == "stale" for i in self.items)


def collect_remotes(report: RepoReport) -> None:
    """Fill in remote names and URLs (with insteadOf rewriting applied)."""
    res = run_git(report.path, "remote")
    if not res.ok:
        report.error = (res.stderr.strip() or "git remote failed").splitlines()[-1]
        return
    for name in res.stdout.split():
        info = RemoteInfo(name)
        url_res = run_git(report.path, "remote", "get-url", name)
        info.url = url_res.stdout.strip() if url_res.ok else ""
        info.endpoint = parse_remote_url(info.url, report.path) if info.url else Endpoint("unknown")
        report.remotes.append(info)


def _ssh_batch_env(config: Dict[str, List[str]]) -> Dict[str, str]:
    """Stop ssh prompting for passwords or host keys, unless the user has
    their own ssh command configured (in which case leave it alone)."""
    if os.environ.get("GIT_SSH_COMMAND") or os.environ.get("GIT_SSH"):
        return {}
    if "core.sshcommand" in config:
        return {}
    return {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15"}


def classify_fetch_error(stderr: str) -> str:
    low = stderr.lower()
    if any(p in low for p in NETWORK_ERROR_PATTERNS):
        return "network"
    if any(p in low for p in AUTH_ERROR_PATTERNS):
        return "auth"
    return "other"


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    # Prefer a "fatal:" line, which is usually the most informative.
    for ln in reversed(lines):
        if ln.lower().startswith(("fatal:", "error:", "ssh:")):
            return ln
    return lines[-1] if lines else "no error output"


@dataclass
class Options:
    branch: str = "main"
    fetch: bool = True
    fetch_timeout: float = 120.0
    expect_remotes: Tuple[str, ...] = ()


class Census:
    """Holds shared state (servers) for the fetch-and-inspect phase."""

    def __init__(self, servers: Dict[str, Server], opts: Options):
        self.servers = servers
        self.opts = opts
        self.lock = threading.Lock()

    # -- fetching ---------------------------------------------------------

    def fetch_remotes(self, report: RepoReport, config: Dict[str, List[str]]) -> None:
        env = _ssh_batch_env(config)
        # Remotes of one repo are fetched one after another: parallel fetches
        # into the same repo can collide on ref locks and FETCH_HEAD.
        for remote in report.remotes:
            ep = remote.endpoint
            if not self.opts.fetch:
                remote.fetch_state = "disabled"
                continue
            if ep is None or not remote.url:
                remote.fetch_state = "skipped"
                remote.fetch_detail = "no URL configured"
                continue
            if ep.kind == "local":
                if not os.path.exists(ep.local_path or ""):
                    remote.fetch_state = "skipped"
                    remote.fetch_detail = f"path not found: {ep.local_path}"
                    continue
            elif ep.kind == "network":
                server = self.servers.get(remote.server_key or "")
                with self.lock:
                    reachable = server.reachable if server else True
                    why = (server.tripped or "homepage did not answer") if server else ""
                if not reachable:
                    remote.fetch_state = "skipped"
                    remote.fetch_detail = f"server offline ({why})"
                    continue
            res = run_git(
                report.path,
                *FETCH_CONFIG,
                *FETCH_ARGS,
                remote.name,
                timeout=self.opts.fetch_timeout,
                env_extra=env,
            )
            if res.ok:
                remote.fetch_state = "fetched" if ep.kind != "unknown" else "unchecked"
                continue
            remote.fetch_state = "failed"
            if res.timed_out:
                kind = "network"
                remote.fetch_detail = f"fetch timed out after {self.opts.fetch_timeout:g}s"
            else:
                kind = classify_fetch_error(res.stderr)
                remote.fetch_detail = _last_line(res.stderr)
            if kind == "network" and ep.kind == "network" and remote.server_key:
                # Circuit breaker: don't keep hammering a server that just
                # failed at the network level.
                with self.lock:
                    server = self.servers.get(remote.server_key)
                    if server and not server.tripped:
                        server.tripped = f"fetch failed in {report.name}: {remote.fetch_detail}"
            elif kind == "auth":
                remote.fetch_detail += (
                    " (hint: check your SSH key/agent or credentials; for a new"
                    " SSH host, connect once by hand to accept its host key)"
                )

    # -- inspection ---------------------------------------------------------

    def process(self, report: RepoReport) -> RepoReport:
        if report.error:
            return report
        cfg_res = run_git(report.path, "config", "-z", "--list")
        config = parse_config_z(cfg_res.stdout) if cfg_res.ok else {}
        self.fetch_remotes(report, config)
        try:
            inspect_repo(report, config, self.opts)
        except Exception as exc:  # keep one odd repo from sinking the run
            report.error = f"internal error while inspecting: {exc!r}"
        return report


def _count(repo: str, *args: str) -> Optional[int]:
    res = run_git(repo, "rev-list", "--count", *args)
    if res.ok:
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None
    return None


def _left_right(repo: str, left: str, right: str) -> Tuple[Optional[int], Optional[int]]:
    res = run_git(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
    if res.ok:
        parts = res.stdout.split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    return None, None


def _ref_changed_time(repo: str, ref: str) -> Optional[int]:
    res = run_git(repo, "log", "-g", "-n1", "--date=unix", "--format=%gd", ref)
    if res.ok:
        m = re.search(r"@\{(\d+)\}", res.stdout)
        if m:
            return int(m.group(1))
    return None


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def format_when(ts: int, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    # Avoid "%-d": a glibc extension that Windows' strftime rejects.
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    age = max(0, now - ts)
    if age < 3600:
        rel = f"{int(age // 60)} min ago"
    elif age < 86400 * 2:
        rel = f"{int(age // 3600)} h ago"
    else:
        rel = f"{int(age // 86400)} days ago"
    return f"{stamp}, {rel}"


def inspect_repo(report: RepoReport, config: Dict[str, List[str]], opts: Options) -> None:
    repo = report.path
    main = opts.branch

    res = run_git(repo, "rev-parse", "--absolute-git-dir")
    if not res.ok:
        report.error = _last_line(res.stderr)
        return
    report.git_dir = res.stdout.strip()

    # HEAD
    res = run_git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    if res.ok:
        report.head_branch = res.stdout.strip()
    else:
        report.detached = True
    report.unborn = not run_git(repo, "rev-parse", "-q", "--verify", "HEAD").ok

    # Operations in progress
    gd = report.git_dir
    ops = []
    if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
        ops.append("merge")
    if os.path.isdir(os.path.join(gd, "rebase-merge")):
        ops.append("rebase")
    if os.path.isdir(os.path.join(gd, "rebase-apply")):
        ops.append("am" if os.path.exists(os.path.join(gd, "rebase-apply", "applying")) else "rebase")
    if os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD")):
        ops.append("cherry-pick")
    if os.path.exists(os.path.join(gd, "REVERT_HEAD")):
        ops.append("revert")
    if os.path.exists(os.path.join(gd, "BISECT_LOG")):
        ops.append("bisect")
    for op in ops:
        report.add("issue", f"a {op} is in progress")

    if report.detached:
        report.add("issue", "HEAD is detached (not on any branch)")
    elif report.head_branch != main:
        report.add("issue", f"checked out on '{report.head_branch}', not '{main}'")

    # Working tree
    res = run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if res.ok:
        st = parse_status_z(res.stdout)
        if st.dirty:
            bits = []
            if st.conflicted:
                bits.append(f"{st.conflicted} conflicted")
            if st.staged:
                bits.append(f"{st.staged} staged")
            if st.unstaged:
                bits.append(f"{st.unstaged} modified")
            if st.untracked:
                bits.append(f"{st.untracked} untracked")
            report.add("issue", "uncommitted changes: " + ", ".join(bits))
    else:
        report.add("issue", f"git status failed: {_last_line(res.stderr)}")

    res = run_git(repo, "stash", "list", "--format=%gd")
    stashes = [ln for ln in res.stdout.splitlines() if ln.strip()] if res.ok else []
    if stashes:
        report.add("issue", f"{_plural(len(stashes), 'stash', 'stashes')} saved")

    # Refs
    res = run_git(
        repo, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)",
        "refs/heads", "refs/remotes",
    )
    local_branches: Dict[str, str] = {}
    remote_refs: Dict[str, Dict[str, str]] = {r.name: {} for r in report.remotes}
    # Longest remote name first, as remote names may contain slashes.
    remote_names = sorted(remote_refs, key=len, reverse=True)
    for line in res.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) < 3:
            continue
        ref, obj, symref = parts[0], parts[1], parts[2]
        if ref.startswith("refs/heads/"):
            local_branches[ref[len("refs/heads/"):]] = obj
        elif ref.startswith("refs/remotes/"):
            rest = ref[len("refs/remotes/"):]
            for rname in remote_names:
                if rest.startswith(rname + "/"):
                    bname = rest[len(rname) + 1:]
                    if not (bname == "HEAD" and symref):
                        remote_refs[rname][bname] = obj
                    break

    has_local_main = main in local_branches
    if not has_local_main and not report.unborn:
        report.add("issue", f"no local '{main}' branch")
    extra_local = sorted(b for b in local_branches if b != main)
    if extra_local:
        report.add("issue", "extra local branch" + ("es" if len(extra_local) > 1 else "")
                   + ": " + ", ".join(extra_local))

    # Tracking
    report.tracking_remote = (config.get(f"branch.{main}.remote") or [None])[-1]
    merge = (config.get(f"branch.{main}.merge") or [None])[-1]
    if merge:
        report.tracking_branch = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else merge
    if has_local_main:
        if not report.tracking_remote:
            report.add("issue", f"'{main}' has no upstream (not tracking any remote)")
        elif report.tracking_remote == ".":
            report.add("issue", f"'{main}' tracks a local branch, not a remote")
        elif report.tracking_remote not in remote_refs:
            report.add("issue", f"'{main}' tracks '{report.tracking_remote}', which is not a configured remote")
        if report.tracking_branch and report.tracking_branch != main:
            report.add("issue", f"'{main}' tracks branch '{report.tracking_branch}' on the remote, not '{main}'")

    if not report.remotes:
        report.add("issue", "no remotes configured")
    for name in opts.expect_remotes:
        if name not in remote_refs:
            report.add("issue", f"expected remote '{name}' is not configured")

    # Unpushed commits (relative to every remote at once)
    if has_local_main:
        unpushed_main = _count(repo, main, "--not", "--remotes")
        if unpushed_main:
            report.add("issue", f"{_plural(unpushed_main, 'commit')} on '{main}' not on any remote")
        if extra_local:
            unpushed_all = _count(repo, "--branches", "--not", "--remotes")
            other = (unpushed_all or 0) - (unpushed_main or 0)
            if other > 0:
                report.add("issue", f"{_plural(other, 'commit')} on other local branches not on any remote")

    # Per remote
    stale_hint = False
    mains_by_remote: Dict[str, str] = {}
    for remote in report.remotes:
        refs = remote_refs.get(remote.name, {})
        remote.branches = sorted(refs)
        remote.has_main = main in refs
        label = remote.name
        reached = remote.fetch_state in ("fetched", "unchecked")
        cached = "" if reached else " (last-known state)"

        if remote.fetch_state == "skipped":
            report.add("stale", f"{label}: not fetched, {remote.fetch_detail}")
        elif remote.fetch_state == "failed":
            report.add("issue", f"{label}: fetch failed: {remote.fetch_detail}")
        elif remote.fetch_state == "unchecked":
            report.add("info", f"{label}: transport could not be connectivity-checked; fetched anyway")

        if not reached and refs:
            remote.ref_changed = _ref_changed_time(repo, f"refs/remotes/{remote.name}/{main}") if remote.has_main else None
            if remote.ref_changed:
                report.add("info", f"{label}: tracking ref last changed {format_when(remote.ref_changed)}")

        if not refs:
            if reached or remote.fetch_state == "disabled":
                report.add("issue", f"{label}: no branches known (empty remote, or never fetched)")
            continue
        if not remote.has_main:
            report.add("issue", f"{label}: has no '{main}' branch{cached}")
        extra = [b for b in remote.branches if b != main]
        if extra:
            report.add("issue", f"{label}: extra branch" + ("es" if len(extra) > 1 else "")
                       + f" {', '.join(extra)}{cached}")
            stale_hint = True
        if remote.has_main:
            mains_by_remote[remote.name] = refs[main]
            if has_local_main:
                remote.ahead, remote.behind = _left_right(repo, main, f"refs/remotes/{remote.name}/{main}")
                bits = []
                if remote.ahead:
                    bits.append(f"{_plural(remote.ahead, 'commit')} to push")
                if remote.behind:
                    bits.append(f"{_plural(remote.behind, 'commit')} to pull")
                if bits:
                    report.add("issue", f"{label}: " + ", ".join(bits) + cached)
    if stale_hint:
        report.add("info", "remote branches are as of the last fetch; ones deleted on the server"
                   " linger until pruned (git fetch --prune <remote>)")

    # Do the remotes agree with each other?
    names = sorted(mains_by_remote)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if mains_by_remote[a] == mains_by_remote[b]:
                continue
            only_a, only_b = _left_right(repo, f"refs/remotes/{a}/{main}", f"refs/remotes/{b}/{main}")
            if only_a is None:
                report.add("issue", f"{a}/{main} and {b}/{main} differ")
                continue
            bits = []
            if only_a:
                bits.append(f"{a} has {_plural(only_a, 'commit')} not on {b}")
            if only_b:
                bits.append(f"{b} has {_plural(only_b, 'commit')} not on {a}")
            report.add("issue", f"{a}/{main} and {b}/{main} differ: " + "; ".join(bits))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def bold(self, t: str) -> str:
        return self._wrap("1", t)


def want_color(mode: str, stream) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    # auto: honour NO_COLOR / FORCE_COLOR, so `repocensus | less -R` can keep
    # colour with FORCE_COLOR=1 (or --color=always).
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


class Progress:
    """One-line progress on stderr, only when stderr is a terminal."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.lock = threading.Lock()

    def show(self, text: str) -> None:
        if self.enabled:
            with self.lock:
                sys.stderr.write("\r\033[K" + text)
                sys.stderr.flush()

    def clear(self) -> None:
        if self.enabled:
            with self.lock:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()


def render(
    root: str,
    reports: Sequence[RepoReport],
    servers: Dict[str, Server],
    opts: Options,
    style: Style,
    quiet: bool,
    warnings: Sequence[str],
    depth_desc: str,
) -> List[str]:
    out: List[str] = []
    out.append(style.bold(f"qdvc-repo-census {__version__}") +
               f" - {root} ({_plural(len(reports), 'repo')}, {depth_desc})")
    for w in warnings:
        out.append(style.yellow(f"warning: {w}"))
    out.append("")

    if not opts.fetch:
        out.append(style.yellow("Fetching disabled (--no-fetch): remote state is as of each repo's last fetch."))
        out.append("")
    elif servers:
        out.append(style.bold("Servers"))
        width = max(len(s.host) for s in servers.values())
        for s in sorted(servers.values(), key=lambda s: s.host):
            names = ", ".join(sorted(s.remote_names))
            alias = f" via {', '.join(sorted(s.aliases))}" if s.aliases else ""
            if s.online and not s.tripped:
                mark, state = style.green("[ok]"), style.green("online ")
            elif s.online and s.tripped:
                mark, state = style.red("[!!]"), style.red("failing")
            else:
                mark, state = style.red("[--]"), style.red("OFFLINE")
            out.append(f"  {mark} {s.host.ljust(width)}  {state}  remotes: {names}{alias};"
                       f" {_plural(s.repo_count, 'repo')}")
            out.append(style.dim(f"         {s.detail}"))
            for note in s.notes:
                out.append(style.dim(f"         {note}"))
            if s.tripped:
                out.append(style.dim(f"         {s.tripped}; later fetches skipped"))
            elif not s.online:
                out.append(style.dim("         not fetched from; last-known state shown instead"))
        out.append("")

    out.append(style.bold("Repositories"))
    width = max((len(r.name) for r in reports), default=0)
    clean = attention = incomplete = 0
    for r in reports:
        if r.error:
            attention += 1
            out.append(f"  {style.red('[!!]')} {r.name.ljust(width)}  {style.red('error: ' + r.error)}")
            continue
        if r.has_issue:
            attention += 1
            mark = style.red("[!!]")
        elif r.has_stale:
            incomplete += 1
            mark = style.yellow("[??]")
        else:
            clean += 1
            if quiet:
                continue
            mark = style.green("[ok]")
        if r.tracking_remote and r.tracking_remote != ".":
            track = f"tracks {r.tracking_remote}/{r.tracking_branch or opts.branch}"
        elif r.tracking_remote == ".":
            track = "tracks a local branch"
        else:
            track = "tracks nothing"
        others = [x.name for x in r.remotes if x.name != r.tracking_remote]
        if others:
            track += f" (also: {', '.join(others)})"
        summary = "" if (r.has_issue or r.has_stale) else style.green("  all clean and in sync")
        out.append(f"  {mark} {r.name.ljust(width)}  {track}{summary}")
        for item in r.items:
            if item.level == "issue":
                out.append(f"       {style.red('-')} {item.text}")
            elif item.level == "stale":
                out.append(f"       {style.yellow('?')} {item.text}")
            else:
                out.append(style.dim(f"         {item.text}"))
    out.append("")

    offline = sum(1 for s in servers.values() if not s.reachable) if opts.fetch else 0
    parts = [f"{_plural(len(reports), 'repo')}", f"{clean} clean", f"{attention} need attention"]
    if incomplete:
        parts.append(f"{incomplete} clean as far as known (some remotes not fetched)")
    if offline:
        parts.append(f"{_plural(offline, 'server')} offline or failing")
    out.append("Summary: " + ", ".join(parts) + ".")
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Report uncommitted changes, unpushed and unpulled commits, tracking"
            " remotes and branch anomalies for every Git repo in a folder. Checks"
            " each server's homepage once and only fetches from servers that"
            " answer. Never pulls, pushes, merges or prunes."
        ),
        epilog=(
            "exit status: 0 all clean and in sync; 1 something needs attention;"
            " 2 usage error or no repos found; 3 nothing flagged but some remotes"
            " could not be fetched."
        ),
    )
    p.add_argument("folder", nargs="?", default=".", help="folder containing the repos (default: .)")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="look for repos at any depth (limit with --depth)")
    p.add_argument("-d", "--depth", type=int, default=None,
                   help="how many folder levels to search (default 1, or unlimited with -r)")
    p.add_argument("-j", "--jobs", type=int, default=4,
                   help="repos to fetch and inspect in parallel (default 4; 1 = one at a time)")
    p.add_argument("-n", "--no-fetch", action="store_true",
                   help="don't check servers or fetch; report from what is already fetched")
    p.add_argument("-b", "--branch", default="main",
                   help="the one branch every repo and remote should have (default: main)")
    p.add_argument("-e", "--expect-remotes", default="",
                   help="comma-separated remote names every repo should have, e.g. alpha,bravo")
    p.add_argument("-q", "--quiet", action="store_true", help="hide repos that are all clean")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="seconds to wait for each server homepage (default 5)")
    p.add_argument("--fetch-timeout", type=float, default=120.0,
                   help="seconds before an individual git fetch is abandoned (default 120)")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="colour output (default auto; NO_COLOR and FORCE_COLOR are honoured)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def plan_servers(reports: Sequence[RepoReport]) -> Dict[str, Server]:
    """Group every network remote across all repos by server host."""
    servers: Dict[str, Server] = {}
    for r in reports:
        hosts_in_repo: Set[str] = set()
        for remote in r.remotes:
            ep = remote.endpoint
            if not ep or ep.kind != "network" or not ep.host:
                continue
            host = resolve_ssh_alias(ep.host) if ep.ssh_style else ep.host
            pages = list(ep.homepages)
            if host != ep.host:
                pages = list(default_homepages(host))
            server = servers.setdefault(host, Server(host))
            for page in pages:
                if page not in server.homepages:
                    server.homepages.append(page)
            server.remote_names.add(remote.name)
            if host != ep.host:
                server.aliases.add(ep.host)
            remote.server_key = host
            hosts_in_repo.add(host)
        for host in hosts_in_repo:
            servers[host].repo_count += 1
    return servers


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if shutil.which("git") is None:
        print(f"{PROG}: git is not installed or not on PATH", file=sys.stderr)
        return EXIT_USAGE
    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"{PROG}: not a folder: {args.folder}", file=sys.stderr)
        return EXIT_USAGE
    if args.depth is not None and args.depth < 1:
        print(f"{PROG}: --depth must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    max_depth = args.depth if args.depth is not None else (None if args.recursive else 1)
    depth_desc = "any depth" if max_depth is None else f"depth {max_depth}"
    jobs = max(1, args.jobs)
    opts = Options(
        branch=args.branch,
        fetch=not args.no_fetch,
        fetch_timeout=args.fetch_timeout,
        expect_remotes=tuple(x.strip() for x in args.expect_remotes.split(",") if x.strip()),
    )
    style = Style(want_color(args.color, sys.stdout))
    progress = Progress(sys.stderr.isatty())

    paths, warnings = find_repos(root, max_depth)
    if not paths:
        print(f"{PROG}: no Git repositories found in {root} ({depth_desc})", file=sys.stderr)
        return EXIT_USAGE
    reports = [RepoReport(p, os.path.relpath(p, root) if p != root else os.path.basename(p)) for p in paths]

    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        # Phase 1: list remotes everywhere (local only, no network).
        progress.show(f"Reading remotes in {_plural(len(reports), 'repo')}...")
        list(executor.map(collect_remotes, reports))

        # Phase 2: check each server's homepage once.
        servers = plan_servers(reports)
        if opts.fetch and servers:
            progress.show(f"Checking {_plural(len(servers), 'server')}...")
            threads = [threading.Thread(target=check_server, args=(s, args.timeout), daemon=True)
                       for s in servers.values()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Phase 3: fetch (where allowed) and inspect each repo.
        census = Census(servers, opts)
        done = 0
        done_lock = threading.Lock()

        def task(report: RepoReport) -> RepoReport:
            nonlocal done
            census.process(report)
            with done_lock:
                done += 1
                verb = "Fetching and inspecting" if opts.fetch else "Inspecting"
                progress.show(f"{verb}: {done}/{len(reports)} repos")
            return report

        list(executor.map(task, reports))
        executor.shutdown(wait=True)
    except KeyboardInterrupt:
        progress.clear()
        executor.shutdown(wait=False, cancel_futures=True)
        kill_all_active()
        print(f"{PROG}: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    progress.clear()

    lines = render(root, reports, servers, opts, style, args.quiet, warnings, depth_desc)
    try:
        print("\n".join(lines))
    except BrokenPipeError:  # e.g. piped into `head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass

    if any(r.has_issue for r in reports):
        return EXIT_ATTENTION
    if any(r.has_stale for r in reports):
        return EXIT_INCOMPLETE
    return EXIT_OK


def main_entry() -> None:
    """Console-script entry point (pyproject `repocensus`)."""
    sys.exit(main())


if __name__ == "__main__":
    main_entry()
