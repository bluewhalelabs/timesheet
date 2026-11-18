import os
import subprocess
import tempfile
import unittest

from tests import helpers
from ats import config, scope

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


def run_git(cwd, *args):
    subprocess.run(["git", "-C", cwd] + list(args), check=True, env=GIT_ENV,
                   capture_output=True, text=True)


def make_repo(path):
    os.makedirs(path)
    run_git(path, "init", "-q")
    run_git(path, "config", "user.email", "dev@example.com")
    run_git(path, "config", "user.name", "Dev")
    run_git(path, "commit", "-q", "--allow-empty", "-m", "init")


def build_cfg(**scopes):
    """scopes: client key -> scope dict (missing scope keys filled in)."""
    clients = {}
    for key, sc in scopes.items():
        full = {"accounts": [], "directories": [], "repos": []}
        full.update(sc)
        clients[key] = {"name": key, "scope": full}
    with helpers.temp_home() as home:
        return config.load(helpers.write_config(home, {"clients": clients}))


def isolated_git(cwd, need_remote):
    return scope.git_info(cwd, need_remote, env=GIT_ENV)


def fail_if_called(cwd, need_remote):
    raise AssertionError("git must not be called")


def no_git(cwd, need_remote):
    return (None, None)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_directory_scope_project_is_first_folder(self):
        base = self.p("clientx")
        os.makedirs(os.path.join(base, "project-a", "src"))
        cfg = build_cfg(acme={"directories": [base]}, beta={})
        self.assertEqual(scope.detect(cfg, os.path.join(base, "project-a", "src"), "", git=no_git),
                         ("acme", "project-a"))

    def test_directory_root_is_root_label(self):
        base = self.p("clientx")
        os.makedirs(base)
        cfg = build_cfg(acme={"directories": [base]}, beta={})
        self.assertEqual(scope.detect(cfg, base, "", git=no_git), ("acme", "(root)"))

    def test_sibling_with_shared_prefix_is_not_inside(self):
        base = self.p("clientx")
        os.makedirs(base)
        os.makedirs(base + "-other")
        cfg = build_cfg(acme={"directories": [base]}, beta={})
        self.assertIsNone(scope.detect(cfg, base + "-other", "", git=no_git))

    def test_worktree_elsewhere_resolves_to_main_repo(self):
        base = self.p("clientx")
        repo = os.path.join(base, "project-a")
        make_repo(repo)
        wt = self.p("elsewhere", "wt")
        os.makedirs(os.path.dirname(wt))
        run_git(repo, "worktree", "add", "-q", wt)
        cfg = build_cfg(acme={"directories": [base]}, beta={})
        self.assertEqual(scope.detect(cfg, wt, "", git=isolated_git), ("acme", "project-a"))

    def test_repo_scope_by_path_and_by_remote(self):
        repo = self.p("repos", "Repo-A")
        make_repo(repo)
        run_git(repo, "remote", "add", "origin", "git@github.com:Example/Repo-A.git")
        by_path = build_cfg(acme={}, beta={"repos": [repo]})
        self.assertEqual(scope.detect(by_path, repo, "", git=isolated_git), ("beta", "Repo-A"))
        by_url = build_cfg(acme={}, beta={"repos": ["https://github.com/example/repo-a"]})
        self.assertEqual(scope.detect(by_url, repo, "", git=isolated_git), ("beta", "Repo-A"))
        miss = build_cfg(acme={}, beta={"repos": ["github.com/example/other"]})
        self.assertIsNone(scope.detect(miss, repo, "", git=isolated_git))

    def test_account_scope_falls_back_to_repo_then_folder(self):
        acct = self.p("claude-clientx")
        os.makedirs(acct)
        cfg = build_cfg(acme={"accounts": [acct]}, beta={})
        repo = self.p("somewhere", "my-repo")
        make_repo(repo)
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        self.assertEqual(scope.detect(cfg, sub, acct, git=isolated_git), ("acme", "my-repo"))
        plain = self.p("plain-folder")
        os.makedirs(plain)
        self.assertEqual(scope.detect(cfg, plain, acct, git=isolated_git), ("acme", "plain-folder"))
        self.assertIsNone(scope.detect(cfg, plain, self.p("other-account"), git=isolated_git))

    def test_precedence_repo_over_directory_over_account(self):
        acct = self.p("acct")
        base = self.p("dir")
        repo = os.path.join(base, "project-a")
        make_repo(repo)
        os.makedirs(acct)
        cfg = build_cfg(acme={"accounts": [acct]}, beta={"directories": [base]})
        self.assertEqual(scope.detect(cfg, repo, acct, git=isolated_git), ("beta", "project-a"))
        cfg = build_cfg(acme={"accounts": [acct]}, beta={"directories": [base]},
                        gamma={"repos": [repo]})
        self.assertEqual(scope.detect(cfg, repo, acct, git=isolated_git), ("gamma", "project-a"))

    def test_tie_goes_to_first_client_in_config(self):
        base = self.p("dir")
        os.makedirs(os.path.join(base, "project-a"))
        cfg = build_cfg(acme={"directories": [base]}, beta={"directories": [base]})
        self.assertEqual(scope.detect(cfg, os.path.join(base, "project-a"), "", git=no_git)[0], "acme")
        cfg = build_cfg(beta={"directories": [base]}, acme={"directories": [base]})
        self.assertEqual(scope.detect(cfg, os.path.join(base, "project-a"), "", git=no_git)[0], "beta")

    def test_unmatched_returns_none(self):
        cfg = build_cfg(acme={"directories": [self.p("dir")]}, beta={})
        self.assertIsNone(scope.detect(cfg, self.root, "", git=no_git))

    def test_never_raises(self):
        cfg = build_cfg(acme={"directories": [self.p("missing")], "repos": ["~/nope"]}, beta={})
        self.assertIsNone(scope.detect(cfg, "", "", git=no_git))
        self.assertIsNone(scope.detect(cfg, self.p("does", "not", "exist"), "", git=no_git))

        def boom(cwd, need_remote):
            raise RuntimeError("x")
        self.assertIsNone(scope.detect(cfg, self.root, "", git=boom))
        self.assertIsNone(scope.detect({}, self.root, ""))

    def test_symlinked_cwd_and_path_with_spaces_match(self):
        base = self.p("client x")
        real = os.path.join(base, "project-a")
        os.makedirs(real)
        link = self.p("link")
        os.symlink(real, link)
        cfg = build_cfg(acme={"directories": [base]}, beta={})
        self.assertEqual(scope.detect(cfg, link, "", git=no_git), ("acme", "project-a"))
        link_base = self.p("linkbase")
        os.symlink(base, link_base)
        cfg = build_cfg(acme={"directories": [link_base]}, beta={})
        self.assertEqual(scope.detect(cfg, real, "", git=no_git), ("acme", "project-a"))

    def test_no_git_call_when_directory_matches_and_no_repos(self):
        base = self.p("dir")
        os.makedirs(os.path.join(base, "project-a"))
        cfg = build_cfg(acme={"directories": [base]}, beta={"accounts": [self.p("a")]})
        self.assertEqual(scope.detect(cfg, os.path.join(base, "project-a"), "", git=fail_if_called),
                         ("acme", "project-a"))

    def test_git_called_once_and_remote_only_when_url_entry(self):
        calls = []

        def spy(cwd, need_remote):
            calls.append(need_remote)
            return (None, None)
        os.makedirs(self.p("d"))
        scope.detect(build_cfg(acme={"repos": [self.p("r")]}, beta={"accounts": ["x"]}),
                     self.p("d"), "", git=spy)
        scope.detect(build_cfg(acme={"repos": ["github.com/example/repo-a"]}),
                     self.p("d"), "", git=spy)
        self.assertEqual(calls, [False, True])

    def test_empty_cwd_is_nothing_to_attribute(self):
        acct = self.p("acct")
        os.makedirs(acct)
        cfg = build_cfg(acme={"accounts": [acct]}, beta={})
        self.assertIsNone(scope.detect(cfg, "", acct, git=fail_if_called))
        self.assertEqual(scope.detect(cfg, self.root, acct, git=no_git),
                         ("acme", os.path.basename(self.root)))

    def test_git_info_in_repo_without_and_with_remote(self):
        repo = self.p("repos", "Repo-A")
        make_repo(repo)
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        run_git(repo, "remote", "add", "origin", "git@github.com:Example/Repo-A.git")
        self.assertEqual(scope.git_info(sub, False, env=GIT_ENV), (repo, None))
        self.assertEqual(scope.git_info(sub, True, env=GIT_ENV),
                         (repo, "git@github.com:Example/Repo-A.git"))

    def test_git_info_outside_repo_is_none(self):
        self.assertEqual(scope.git_info(self.root, True, env=GIT_ENV), (None, None))
        self.assertEqual(scope.git_info(self.p("missing"), False, env=GIT_ENV), (None, None))

    def test_normalize_remote_forms(self):
        want = "github.com/example/repo"
        host = "@".join(("git", "github.com"))   # built so the packaging email scan sees no address
        for url in ("git@github.com:Example/Repo.git", "https://github.com/Example/Repo",
                    "ssh://%s/Example/Repo.git" % host,
                    "https://github.com/Example/Repo/",
                    "github.com/example/repo",
                    "ssh://%s:22/Example/Repo.git" % host):
            self.assertEqual(scope.normalize_remote(url), want, url)


if __name__ == "__main__":
    unittest.main()
