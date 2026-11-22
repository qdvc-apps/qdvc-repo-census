"""Tests for repocensus. Run with:  python3 -m unittest discover -s tests

Integration tests build real repos with file-path remotes in a temp folder.
Git's global and system config are isolated so the developer's own settings
(pull.rebase, init.defaultBranch, aliases...) cannot change the results.
"""

import http.server
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import repocensus as rc  # noqa: E402

PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_saved_env = {}


def setUpModule():
    global _home
    _home = tempfile.mkdtemp(prefix="rc-home-")
    iso = {
        "HOME": _home,
        "GIT_CONFIG_GLOBAL": os.path.join(_home, "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "NO_COLOR": "1",
    }
    for k, v in iso.items():
        _saved_env[k] = os.environ.get(k)
        os.environ[k] = v
    # Proxies make "is this host up?" non-deterministic; tests run direct.
    for k in PROXY_VARS:
        _saved_env[k] = os.environ.pop(k, None)
    with open(iso["GIT_CONFIG_GLOBAL"], "w") as f:
        f.write("[init]\n\tdefaultBranch = main\n")


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_home, ignore_errors=True)


def git(cwd, *args):
    res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout.strip()


def commit(repo, name, content="x"):
    with open(os.path.join(repo, name), "w") as f:
        f.write(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")


# --------------------------------------------------------------------------


class ParseRemoteUrlTests(unittest.TestCase):
    def test_https(self):
        ep = rc.parse_remote_url("https://user@Git.Example.com/team/repo.git")
        self.assertEqual(ep.kind, "network")
        self.assertEqual(ep.host, "git.example.com")
        self.assertEqual(ep.homepages, ("https://git.example.com/", "http://git.example.com/"))

    def test_http_with_port(self):
        ep = rc.parse_remote_url("http://site-office:8080/git/repo.git")
        self.assertEqual(ep.homepages[0], "http://site-office:8080/")
        self.assertIn("https://site-office/", ep.homepages)

    def test_scp_like(self):
        ep = rc.parse_remote_url("git@bravo.example.org:team/repo.git")
        self.assertEqual((ep.kind, ep.host, ep.ssh_style), ("network", "bravo.example.org", True))

    def test_scp_like_alias_without_user(self):
        ep = rc.parse_remote_url("alpha:repos/thing.git")
        self.assertEqual(ep.host, "alpha")
        self.assertTrue(ep.ssh_style)

    def test_ssh_url_with_port(self):
        ep = rc.parse_remote_url("ssh://git@10.0.0.5:2222/srv/repo.git")
        self.assertEqual(ep.host, "10.0.0.5")
        self.assertEqual(ep.homepages, ("https://10.0.0.5/", "http://10.0.0.5/"))

    def test_ipv6(self):
        ep = rc.parse_remote_url("ssh://git@[fe80::1]/repo.git")
        self.assertEqual(ep.host, "fe80::1")
        self.assertEqual(ep.homepages[0], "https://[fe80::1]/")

    def test_git_protocol(self):
        self.assertEqual(rc.parse_remote_url("git://example.net/repo.git").host, "example.net")

    def test_file_url(self):
        ep = rc.parse_remote_url("file:///srv/git/repo.git")
        self.assertEqual((ep.kind, ep.local_path), ("local", os.path.normpath("/srv/git/repo.git")))

    def test_relative_path(self):
        ep = rc.parse_remote_url("../bare/repo.git", "/work/clone")
        self.assertEqual(ep.local_path, os.path.normpath("/work/bare/repo.git"))

    def test_path_with_colon_after_slash_is_local(self):
        self.assertEqual(rc.parse_remote_url("./odd:name").kind, "local")

    def test_windows_drive_is_local(self):
        self.assertEqual(rc.parse_remote_url("C:\\repos\\thing").kind, "local")

    def test_transport_syntax_unknown(self):
        self.assertEqual(rc.parse_remote_url("ext::ssh -i key host %S repo").kind, "unknown")


class ParseStatusTests(unittest.TestCase):
    def test_counts(self):
        data = "M  staged.txt\0 M changed.txt\0MM both.txt\0?? new.txt\0UU clash.txt\0!! ignored\0"
        st = rc.parse_status_z(data)
        self.assertEqual((st.staged, st.unstaged, st.untracked, st.conflicted), (2, 2, 1, 1))

    def test_rename_consumes_original_path(self):
        st = rc.parse_status_z("R  new.txt\0old.txt\0?? x\0")
        self.assertEqual((st.staged, st.untracked), (1, 1))

    def test_empty(self):
        self.assertFalse(rc.parse_status_z("").dirty)


class MiscTests(unittest.TestCase):
    def test_config_parse(self):
        cfg = rc.parse_config_z("branch.main.remote\nalpha\0remote.alpha.fetch\na\0remote.alpha.fetch\nb\0")
        self.assertEqual(cfg["branch.main.remote"], ["alpha"])
        self.assertEqual(cfg["remote.alpha.fetch"], ["a", "b"])

    def test_classify(self):
        self.assertEqual(rc.classify_fetch_error("ssh: connect to host x port 22: Connection timed out"), "network")
        self.assertEqual(rc.classify_fetch_error("git@x: Permission denied (publickey)."), "auth")
        self.assertEqual(rc.classify_fetch_error("fatal: something odd"), "other")

    def test_format_when_has_no_platform_specific_codes(self):
        self.assertIn("days ago", rc.format_when(0, now=10 * 86400))

    def test_ssh_batch_env_respects_user_config(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SSH_COMMAND", None)
            os.environ.pop("GIT_SSH", None)
            self.assertIn("BatchMode=yes", rc._ssh_batch_env({})["GIT_SSH_COMMAND"])
            self.assertEqual(rc._ssh_batch_env({"core.sshcommand": ["ssh -i k"]}), {})

    def test_uses_proxy(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy:3128", "no_proxy": "internal.lan"}):
            self.assertTrue(rc._uses_proxy("https://example.com/"))
            self.assertFalse(rc._uses_proxy("https://internal.lan/"))
            self.assertFalse(rc._uses_proxy("https://git.internal.lan/"))

    def test_no_proxy_cidr_like_curl(self):
        # urllib ignores CIDR entries; git (via curl) honours them.
        env = {"https_proxy": "http://proxy:3128", "no_proxy": "10.0.0.0/8,fd00::/8,*.corp.lan"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("NO_PROXY", None)
            self.assertIsNone(rc.proxy_for("https://10.20.30.40/"))
            self.assertIsNone(rc.proxy_for("https://[fd00::5]/"))
            self.assertIsNone(rc.proxy_for("https://site.corp.lan/"))
            self.assertEqual(rc.proxy_for("https://11.0.0.1/"), "http://proxy:3128")

    def test_no_proxy_matcher(self):
        m = rc.no_proxy_matches
        self.assertTrue(m("anything", ["*"]))
        self.assertTrue(m("a.b.example", [".example"]))
        self.assertTrue(m("example", ["example:8080"]))
        self.assertFalse(m("notexample", ["example"]))
        self.assertTrue(m("192.168.1.9", ["192.168.0.0/16"]))
        self.assertFalse(m("192.169.1.9", ["192.168.0.0/16"]))


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 404

    def do_HEAD(self):
        self.send_response(self.status)
        self.end_headers()

    do_GET = do_HEAD

    def log_message(self, *a):
        pass


class HomepageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_any_http_status_is_online(self):
        ok, detail = rc.check_homepage(f"http://127.0.0.1:{self.port}/", 3)
        self.assertTrue(ok)
        self.assertIn("404", detail)

    def test_closed_port_is_offline(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ok, detail = rc.check_homepage(f"http://127.0.0.1:{port}/", 3)
        self.assertFalse(ok)
        self.assertIn("refused", detail)

    def test_http_server_on_https_url_counts_as_answered(self):
        # A plain-HTTP server answering a TLS hello: something is listening.
        ok, _ = rc.check_homepage(f"https://127.0.0.1:{self.port}/", 3)
        self.assertTrue(ok)

    def test_unresolvable_host_is_offline(self):
        ok, _ = rc.check_homepage("http://no-such-host.invalid/", 3)
        self.assertFalse(ok)


# --------------------------------------------------------------------------


class IntegrationTests(unittest.TestCase):
    """Builds a folder of clones against two bare 'servers' on disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-")
        self.servers = os.path.join(self.tmp, "servers")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.servers)
        os.makedirs(self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_project(self, name, remotes=("alpha", "bravo"), track="alpha"):
        """Create bare repos per remote and a clone in work/ that pushes to all."""
        clone = os.path.join(self.work, name)
        os.makedirs(clone)
        git(clone, "init", "-q", "-b", "main")
        commit(clone, "README", name)
        for r in remotes:
            bare = os.path.join(self.servers, r, name + ".git")
            os.makedirs(os.path.dirname(bare), exist_ok=True)
            git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
            git(clone, "remote", "add", r, bare)
            git(clone, "push", "-q", r, "main")
        if track:
            git(clone, "branch", "-q", f"--set-upstream-to={track}/main", "main")
        return clone

    def bare(self, remote, name):
        return os.path.join(self.servers, remote, name + ".git")

    def push_from_elsewhere(self, remote, name, filename):
        other = os.path.join(self.tmp, "other-" + name + "-" + filename)
        git(self.tmp, "clone", "-q", self.bare(remote, name), other)
        commit(other, filename)
        git(other, "push", "-q", "origin", "main")

    def census(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main([self.work, "--color=never", *extra])
        return code, out.getvalue()

    def section(self, text, repo):
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if f"] {repo} " in ln or ln.rstrip().endswith(f"] {repo}"))
        block = [lines[start]]
        for ln in lines[start + 1:]:
            if ln.startswith("  [") or not ln.strip():
                break
            block.append(ln)
        return "\n".join(block)

    def test_clean(self):
        self.make_project("clean")
        code, out = self.census()
        self.assertEqual(code, rc.EXIT_OK, out)
        self.assertIn("[ok] clean", out)
        self.assertIn("tracks alpha/main (also: bravo)", out)

    def test_dirty_and_stash(self):
        repo = self.make_project("dirty")
        commit(repo, "a.txt")
        git(repo, "push", "-q", "alpha", "main")
        git(repo, "push", "-q", "bravo", "main")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("changed")
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("n")
        git(repo, "stash", "-q")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("changed again")
        code, out = self.census()
        self.assertEqual(code, rc.EXIT_ATTENTION)
        sec = self.section(out, "dirty")
        self.assertIn("1 modified, 1 untracked", sec)
        self.assertIn("1 stash saved", sec)

    def test_unpushed_and_unpulled(self):
        repo = self.make_project("sync")
        commit(repo, "local.txt")  # unpushed everywhere
        self.push_from_elsewhere("alpha", "sync", "upstream.txt")  # unpulled from alpha
        code, out = self.census()
        sec = self.section(out, "sync")
        self.assertIn("1 commit on 'main' not on any remote", sec)
        self.assertIn("alpha: 1 commit to push, 1 commit to pull", sec)
        self.assertIn("bravo: 1 commit to push", sec)
        self.assertIn("alpha/main and bravo/main differ: alpha has 1 commit not on bravo", sec)
        self.assertEqual(code, rc.EXIT_ATTENTION)

    def test_no_fetch_does_not_see_new_upstream(self):
        self.make_project("nofetch")
        self.push_from_elsewhere("alpha", "nofetch", "upstream.txt")
        code, out = self.census("--no-fetch")
        self.assertEqual(code, rc.EXIT_OK, out)
        self.assertIn("Fetching disabled", out)

    def test_branch_anomalies(self):
        repo = self.make_project("branches")
        git(repo, "branch", "-q", "feature")
        git(repo, "push", "-q", "bravo", "feature")
        code, out = self.census()
        sec = self.section(out, "branches")
        self.assertIn("extra local branch: feature", sec)
        self.assertIn("bravo: extra branch feature", sec)

    def test_wrong_tracking_and_detached(self):
        repo = self.make_project("detached", track=None)
        git(repo, "checkout", "-q", "--detach")
        code, out = self.census()
        sec = self.section(out, "detached")
        self.assertIn("HEAD is detached", sec)
        self.assertIn("'main' has no upstream", sec)
        self.assertIn("tracks nothing", sec)

    def test_offline_server_skipped_and_last_known_shown(self):
        repo = self.make_project("offline", remotes=("alpha",))
        # A second remote on an unreachable host, with refs from an "earlier" fetch.
        git(repo, "remote", "add", "bravo", "git@no-such-host.invalid:team/offline.git")
        git(repo, "update-ref", "refs/remotes/bravo/main", "HEAD")
        code, out = self.census("--timeout", "2")
        self.assertIn("no-such-host.invalid", out)
        self.assertIn("OFFLINE", out)
        sec = self.section(out, "offline")
        self.assertIn("bravo: not fetched, server offline", sec)
        self.assertIn("[??]", sec)
        self.assertEqual(code, rc.EXIT_INCOMPLETE, out)

    def test_missing_local_remote_path(self):
        repo = self.make_project("gone", remotes=("alpha",))
        shutil.rmtree(self.bare("alpha", "gone"))
        code, out = self.census()
        self.assertIn("alpha: not fetched, path not found", self.section(out, "gone"))

    def test_fetch_failure_reported(self):
        repo = self.make_project("broken", remotes=("alpha",))
        bad = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(bad)
        git(repo, "remote", "set-url", "alpha", bad)
        code, out = self.census()
        self.assertIn("alpha: fetch failed", self.section(out, "broken"))
        self.assertEqual(code, rc.EXIT_ATTENTION)

    def test_expect_remotes_and_wrong_branch_name(self):
        repo = os.path.join(self.work, "master-only")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "master")
        commit(repo, "f")
        code, out = self.census("--expect-remotes", "alpha,bravo")
        sec = self.section(out, "master-only")
        self.assertIn("checked out on 'master', not 'main'", sec)
        self.assertIn("no local 'main' branch", sec)
        self.assertIn("expected remote 'alpha' is not configured", sec)
        self.assertIn("no remotes configured", sec)

    def test_recursive_and_depth(self):
        self.make_project("top")
        nested_parent = os.path.join(self.work, "group")
        os.makedirs(nested_parent)
        nested = os.path.join(nested_parent, "deep")
        os.makedirs(nested)
        git(nested, "init", "-q", "-b", "main")
        commit(nested, "f")
        _, out1 = self.census()
        self.assertNotIn("deep", out1)
        _, out2 = self.census("-r")
        self.assertIn(os.path.join("group", "deep"), out2)

    def test_quiet_hides_clean(self):
        self.make_project("clean")
        repo = self.make_project("dirty2")
        with open(os.path.join(repo, "u.txt"), "w") as f:
            f.write("u")
        _, out = self.census("-q", "-j", "1")
        self.assertNotIn("[ok] clean", out)
        self.assertIn("dirty2", out)

    def test_status_does_not_take_index_lock(self):
        # GIT_OPTIONAL_LOCKS=0 must be in effect for our git calls.
        self.assertEqual(rc._git_env()["GIT_OPTIONAL_LOCKS"], "0")

    def test_no_repos(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(rc.main([empty]), rc.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
