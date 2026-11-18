"""Scope detection: which client and project a working directory belongs to."""
import os
import re
import subprocess
from typing import Callable, List, Optional, Tuple

from ats import config

__all__ = ["git_info", "normalize_remote", "detect"]

ROOT_LABEL = "(root)"


def git_info(cwd: str, need_remote: bool,
             env: Optional[dict] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (main_repo_path, origin_url); (None, None) on any failure.
    env, when given, is the environment for the git subprocesses."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=2, env=env)
        out = r.stdout.strip()
        if not out:
            return (None, None)
        if out.endswith("/.git"):
            main = os.path.dirname(out)
        else:
            main = out
        main = os.path.realpath(main)
        url = None
        if need_remote:
            r2 = subprocess.run(
                ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=2, env=env)
            url = r2.stdout.strip() or None
        return (main, url)
    except Exception:
        return (None, None)


def normalize_remote(url: str) -> str:
    """github.com/org/repo form of a git remote URL (lowercase, no .git)."""
    u = (url or "").strip()
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^(?:[^@/]*@)?([^/:]+):(.*)$", u)
        if m:
            host, path = m.group(1), m.group(2)
        else:
            host, path = "", u
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    out = "%s/%s" % (host, path) if host else path
    return out.lower()


def _is_path_entry(entry: str) -> bool:
    return entry.startswith(("/", "~", "."))


def _inside(path: str, directory: str) -> bool:
    if not path or not directory:
        return False
    if path == directory:
        return True
    prefix = directory if directory.endswith(os.sep) else directory + os.sep
    return path.startswith(prefix)


def _first_folder(path: str, directory: str) -> str:
    rel = os.path.relpath(path, directory)
    if rel == ".":
        return ROOT_LABEL
    return rel.split(os.sep)[0]


def _dir_project(dirs: List[str], cwd: str, main: Optional[str]) -> Optional[str]:
    """Project under the first of dirs that contains cwd, else the main repo."""
    for base in (cwd, main):
        if not base:
            continue
        for d in dirs:
            if _inside(base, d):
                return _first_folder(base, d)
    return None


def _scope(client: dict, key: str) -> List[str]:
    return list(client.get("scope", {}).get(key, []))


def _detect(cfg: dict, cwd: str, account: str,
            git: Callable) -> Optional[Tuple[str, str]]:
    keys = config.client_keys(cfg)
    clients = cfg.get("clients", {})
    cwd_real = config.expand(cwd)
    acct = config.expand(account)

    dirs = {k: [d for d in (config.expand(x) for x in _scope(clients[k], "directories")) if d]
            for k in keys}
    has_repos = any(_scope(clients[k], "repos") for k in keys)
    need_remote = any(not _is_path_entry(e) for k in keys
                      for e in _scope(clients[k], "repos"))

    cwd_dir_hit = any(_inside(cwd_real, d) for k in keys for d in dirs[k])
    main = origin = None
    if cwd_real and (has_repos or not cwd_dir_hit):
        try:
            main, origin = git(cwd_real, need_remote)
        except Exception:
            main, origin = None, None
        main = os.path.realpath(main) if main else None
    origin_norm = normalize_remote(origin) if origin else ""

    best = None  # (level, index)
    for i, k in enumerate(keys):
        client = clients[k]
        level = 0
        for e in _scope(client, "repos"):
            if _is_path_entry(e):
                if main and config.expand(e) == main:
                    level = 3
            elif origin_norm and normalize_remote(e) == origin_norm:
                level = 3
        if level < 3 and any(_inside(cwd_real, d) or _inside(main, d) for d in dirs[k]):
            level = 2
        if level < 1 and acct and any(config.expand(a) == acct
                                      for a in _scope(client, "accounts")):
            level = 1
        if level and (best is None or level > best[0]):
            best = (level, i)
    if best is None:
        return None

    level, i = best
    k = keys[i]
    if level == 3:
        name = os.path.basename(main) if main else ""
        return (k, name or os.path.basename(cwd_real))
    proj = _dir_project(dirs[k], cwd_real, main)
    if proj:
        return (k, proj)
    if main and os.path.basename(main):
        return (k, os.path.basename(main))
    return (k, os.path.basename(cwd_real.rstrip(os.sep)) or ROOT_LABEL)


def detect(cfg: dict, cwd: str, account: str,
           git: Callable = git_info) -> Optional[Tuple[str, str]]:
    """(client, project) for cwd under account, or None. Never raises."""
    if not cwd:
        return None
    try:
        return _detect(cfg, cwd, account, git)
    except Exception:
        return None
