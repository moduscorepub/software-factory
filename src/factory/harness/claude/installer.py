"""`factory install claude`: register the packaged plugin through a local marketplace.

Installing as a Claude Code plugin keeps the user's global configuration untouched; `factory work`
loads the same plugin per-session with --plugin-dir when it is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).parent
PLUGIN_DIR = HARNESS / "plugin"
MARKETPLACE = "factory-local"
PLUGIN_ID = f"factory@{MARKETPLACE}"


def _claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", *args], capture_output=True, text=True)


def installed() -> bool:
    if not shutil.which("claude"):
        return False
    try:
        plugins = json.loads(_claude("plugin", "list", "--json").stdout)
    except json.JSONDecodeError:
        return False
    return any(p.get("id") == PLUGIN_ID and p.get("enabled") for p in plugins)


def install() -> str:
    if not shutil.which("claude"):
        raise RuntimeError("`claude` CLI not found on PATH")
    _claude("plugin", "marketplace", "remove", MARKETPLACE)  # re-point at this installation
    for args in (("plugin", "marketplace", "add", str(HARNESS)), ("plugin", "install", PLUGIN_ID)):
        proc = _claude(*args)
        if proc.returncode != 0:
            raise RuntimeError(f"claude {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return f"installed {PLUGIN_ID} from {HARNESS}"


def factory_bin() -> str:
    """The factory executable hooks should call (hooks.json uses ${FACTORY_BIN:-factory})."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "factory" and argv0.exists():
        return str(argv0.absolute())
    return shutil.which("factory") or "factory"
