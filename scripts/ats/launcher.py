"""The optional Claude Code status line: a stable launcher script in the data dir and
the `statusLine` entry in a Claude settings file.

Claude Code has one `statusLine` setting per account, so installing is explicit and
never replaces somebody else's status line unless forced."""
import json
import os
import shlex
import shutil
import stat
import tempfile
from typing import Optional, Tuple

from ats import config, status

__all__ = ["LauncherError", "BACKUP_SUFFIX", "launcher_path", "command", "snippet",
           "settings_path", "write_launcher", "install", "uninstall"]

BACKUP_SUFFIX = ".bak-timesheet"
REFRESH_SECONDS = 60

_SCRIPT = """#!/bin/sh
# timesheet status line launcher, written by `timesheet.py statusline --install`.
# Reads where the plugin lives from plugin-root (kept current by the plugin's
# SessionStart hook) and runs its status line script. Prints nothing if either is missing.
root_file=%s
[ -r "$root_file" ] || exit 0
root=
IFS= read -r root < "$root_file" || [ -n "$root" ] || exit 0
[ -n "$root" ] || exit 0
[ -f "$root/scripts/statusline.py" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 "$root/scripts/statusline.py"
"""


class LauncherError(Exception):
    pass


def _home() -> str:
    return os.path.abspath(config.home())


def launcher_path() -> str:
    return os.path.join(_home(), "statusline.sh")


def command() -> str:
    """The launcher as a shell command (quoted only when the path needs it)."""
    return shlex.quote(launcher_path())


def snippet() -> dict:
    return {"statusLine": {"type": "command", "command": command(),
                           "refreshInterval": REFRESH_SECONDS}}


def settings_path(path: Optional[str] = None) -> str:
    """The settings file to edit: the given one, else settings.json in the Claude
    config dir (CLAUDE_CONFIG_DIR, default ~/.claude)."""
    if path:
        return os.path.abspath(os.path.expanduser(path))
    return os.path.join(os.path.abspath(os.path.expanduser(status.account_dir())),
                        "settings.json")


def write_launcher(plugin_root: str) -> str:
    """Write <home>/statusline.sh (0700) and <home>/plugin-root; returns the launcher."""
    home = _home()
    os.makedirs(home, mode=0o700, exist_ok=True)
    path = launcher_path()
    status.write_private(path, _SCRIPT % shlex.quote(os.path.join(home, "plugin-root")), 0o700)
    status.record_plugin_root(plugin_root)
    return path


def _read(path: str) -> Tuple[Optional[str], dict]:
    """(previous text or None when there is no file, parsed settings)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None, {}
    except (OSError, UnicodeDecodeError) as e:
        raise LauncherError("cannot read %s: %s" % (path, e))
    if not text.strip():
        return text, {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise LauncherError("%s is not valid JSON (%s); it was left untouched" % (path, e))
    if not isinstance(data, dict):
        raise LauncherError("%s does not hold a JSON object; it was left untouched" % path)
    return text, data


def _write(path: str, previous: Optional[str], data: dict) -> None:
    """Atomically replace the settings file, keeping its mode and a copy of the old one."""
    target = os.path.realpath(path)             # a symlinked settings file stays a symlink
    d = os.path.dirname(target)
    try:
        os.makedirs(d, exist_ok=True)
        mode = 0o600
        if previous is not None:
            mode = stat.S_IMODE(os.stat(target).st_mode)
            shutil.copy2(target, path + BACKUP_SUFFIX)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise LauncherError("cannot write %s: %s" % (path, e.strerror or e))


def _is_ours(entry) -> bool:
    return isinstance(entry, dict) and entry.get("command") in (command(), launcher_path())


def _describe(entry) -> str:
    if isinstance(entry, dict) and isinstance(entry.get("command"), str):
        return entry["command"]
    return json.dumps(entry, ensure_ascii=False)


def install(path: str, plugin_root: str, force: bool = False) -> str:
    """Point `statusLine` in the settings file at the launcher. Raises LauncherError
    (settings untouched) for unreadable settings or somebody else's status line."""
    previous, data = _read(path)
    launcher = write_launcher(plugin_root)
    wanted = snippet()["statusLine"]
    existing = data.get("statusLine")
    if existing == wanted:
        return "status line already installed in %s" % path
    if existing is not None and not _is_ours(existing) and not force:
        raise LauncherError(
            "%s already has a different status line:\n  %s\n"
            "It was not changed (Claude Code has one status line per account). To add the "
            "timesheet line to yours, run the launcher from your script with the "
            "same JSON on stdin and print what it prints (one line, or nothing):\n"
            "  %s\n"
            "To replace yours instead, run this command again with --force."
            % (path, _describe(existing), command()))
    data["statusLine"] = wanted
    _write(path, previous, data)
    out = "status line installed in %s (launcher: %s)" % (path, launcher)
    if previous is not None:
        out += "\nprevious settings kept in %s" % (path + BACKUP_SUFFIX)
    return out


def uninstall(path: str) -> str:
    """Remove `statusLine` when it is ours (and the launcher); leave anything else."""
    previous, data = _read(path)
    existing = data.get("statusLine")
    if existing is None:
        return "no status line is set in %s; nothing to remove" % path
    if not _is_ours(existing):
        return ("the status line in %s is not timesheet's (%s); left as it is"
                % (path, _describe(existing)))
    del data["statusLine"]
    _write(path, previous, data)
    try:
        os.unlink(launcher_path())
    except OSError:
        pass
    return "status line removed from %s\nprevious settings kept in %s" % (
        path, path + BACKUP_SUFFIX)
