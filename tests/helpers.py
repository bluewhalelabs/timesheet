"""Shared test helpers. Tests never touch the real data dir."""
import contextlib
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

# Code run in-process reads the data dir (the engine loads pauses.json from it). Without
# an explicit home, point it at a directory that does not exist, never at the real one.
if not os.environ.get("TIMESHEET_HOME"):
    os.environ["TIMESHEET_HOME"] = os.path.join(tempfile.gettempdir(),
                                                "timesheet-tests-no-such-home")


@contextlib.contextmanager
def temp_home():
    """Yield a temp dir with TIMESHEET_HOME pointing at it, then restore."""
    old = os.environ.get("TIMESHEET_HOME")
    with tempfile.TemporaryDirectory() as d:
        home = os.path.realpath(d)
        os.environ["TIMESHEET_HOME"] = home
        try:
            yield home
        finally:
            if old is None:
                os.environ.pop("TIMESHEET_HOME", None)
            else:
                os.environ["TIMESHEET_HOME"] = old


def write_config(home, cfg):
    """Write cfg as <home>/config.json and return its path."""
    path = os.path.join(home, "config.json")
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    return path
