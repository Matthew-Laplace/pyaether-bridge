"""Central configuration for the PyAether bridge (host side, stdlib only).

Nothing here is machine specific: values come from environment variables, then
from an optional user config file, then from generic defaults. That keeps local
paths, container names and license endpoints out of the repository.

Environment variables
    PYAETHER_TRANSPORT       docker | ssh | local       (default docker)
    PYAETHER_CONTAINER       docker container name      (default empyrean-gui)
    PYAETHER_SSH_HOST        user@server                (transport=ssh)
    PYAETHER_SSH_PORT        ssh port                   (optional)
    PYAETHER_SSH_OPTS        extra ssh options          (optional)
    PYAETHER_PYTHON          interpreter on the target  (default python3.9)
    PYAETHER_REMOTE_DIR      where the session script is deployed
                             (default /tmp/pyaether-bridge)
    PYAETHER_LICENSE_SERVER  value for LM_LICENSE_FILE on the target; unset means
                             "keep whatever the target already has"
    PYAETHER_BRIDGE_HOME     data directory  (default ~/.cache/pyaether-bridge)
    PYAETHER_CATALOG_DB      catalog sqlite path
    PYAETHER_DAEMON_SOCK     host daemon unix socket path
    PYAETHER_DOCS_DIR        Sphinx docs/html directory used by `api build`
    PYAETHER_BRIDGE_NO_AUTOSTART  set to 1 to forbid auto-starting the daemon

User config file (optional): <DATA_DIR>/config.json, same keys in snake_case
    {"transport": "ssh", "ssh_host": "user@server", "container": "...",
     "python": "...", "remote_dir": "...", "license_server": "...",
     "docs_dir": "...", "catalog_db": "..."}
"""

from __future__ import annotations

import json
import os
import pathlib

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def _env_path(name: str):
    value = os.environ.get(name, "")
    return pathlib.Path(value).expanduser() if value else None


DATA_DIR = _env_path("PYAETHER_BRIDGE_HOME") or (pathlib.Path.home() / ".cache" / "pyaether-bridge")


def _load_user_config():
    """Optional <DATA_DIR>/config.json — machine-specific settings live here so
    that local paths, container names and license endpoints never enter the
    repository."""
    try:
        with open(DATA_DIR / "config.json", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


USER_CONFIG = _load_user_config()


def setting(env_name, key, default=None):
    """Resolution order: environment variable > user config file > default."""
    value = os.environ.get(env_name)
    if value:
        return value
    value = USER_CONFIG.get(key)
    if value:
        return value
    return default


TRANSPORT = setting("PYAETHER_TRANSPORT", "transport", "docker")
CONTAINER = setting("PYAETHER_CONTAINER", "container", "empyrean-gui")
SSH_HOST = setting("PYAETHER_SSH_HOST", "ssh_host", "")
SSH_PORT = setting("PYAETHER_SSH_PORT", "ssh_port", "")
SSH_OPTS = setting("PYAETHER_SSH_OPTS", "ssh_opts", "")
PYTHON = setting("PYAETHER_PYTHON", "python", "python3.9")
REMOTE_DIR = setting("PYAETHER_REMOTE_DIR", "remote_dir", "/tmp/pyaether-bridge")
SESSION_SCRIPT_SRC = PACKAGE_DIR / "session_bridge.py"

# Empty means "do not override the target's own LM_LICENSE_FILE" (most
# deployments already set it).
LICENSE_SERVER = setting("PYAETHER_LICENSE_SERVER", "license_server")

DAEMON_SOCK = _env_path("PYAETHER_DAEMON_SOCK") or (DATA_DIR / "daemon.sock")
DAEMON_PID = DATA_DIR / "daemon.pid"
DAEMON_LOG = DATA_DIR / "daemon.log"

# Catalog lives in the project so the whole bridge stays self-contained;
# PYAETHER_CATALOG_DB overrides it (tests point it at a temp file).
_catalog_override = _env_path("PYAETHER_CATALOG_DB") or (
    pathlib.Path(USER_CONFIG["catalog_db"]).expanduser()
    if USER_CONFIG.get("catalog_db") else None
)
CATALOG_DB = _catalog_override or (PROJECT_DIR / "data" / "catalog.sqlite")

# `api build` only needs the docs that ship inside an installed Aether tree, so
# discovery is version agnostic: $AETHER_ROOT first, then common install roots.
_DOCS_TAIL = pathlib.Path("tools/pyaether/docs/html")


def _docs_candidates():
    candidates = []
    for value in (os.environ.get("PYAETHER_DOCS_DIR"), USER_CONFIG.get("docs_dir")):
        if value:
            candidates.append(pathlib.Path(value).expanduser())
    aether_root = os.environ.get("AETHER_ROOT") or USER_CONFIG.get("aether_root")
    if aether_root:
        candidates.append(pathlib.Path(aether_root).expanduser() / _DOCS_TAIL)
    for base in (pathlib.Path("/opt/empyrean"), pathlib.Path.home() / "empyrean"):
        try:
            candidates.extend(sorted(base.glob(str(pathlib.Path("*") / _DOCS_TAIL))))
        except OSError:
            pass
    candidates.append(DATA_DIR / "docs/html")
    return candidates

NO_AUTOSTART = os.environ.get("PYAETHER_BRIDGE_NO_AUTOSTART", "") not in ("", "0", "false")


def find_docs_dir():
    """Return the first existing docs/html directory, or None."""
    for candidate in _docs_candidates():
        if candidate.is_dir() and (candidate / "objects.inv").exists():
            return candidate
    return None
