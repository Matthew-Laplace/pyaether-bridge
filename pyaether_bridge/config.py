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

    PYAETHER_SIM_TARGET      where simulators run: docker / ssh / local
                             (default: the bridge transport)
    PYAETHER_SIM_BACKEND     default simulator backend (default ngspice)
    PYAETHER_SIM_WORKDIR     run directory root on the simulator target
    PYAETHER_SIM_TIMEOUT     default simulation timeout in seconds (default 600)
    PYAETHER_NGSPICE_BIN     ngspice binary name or path
    PYAETHER_SPECTRE_BIN     Spectre binary name or path
    PYAETHER_SIM_CMD         command template for the "custom" backend
    PYAETHER_PROFILE         name of the profile to activate

User config file (optional): <DATA_DIR>/config.json, same keys in snake_case
    {"transport": "ssh", "ssh_host": "user@server", "container": "...",
     "python": "...", "remote_dir": "...", "license_server": "...",
     "docs_dir": "...", "catalog_db": "...",
     "sim_target": "local", "sim_backend": "ngspice", "sim_timeout": 600,
     "default_profile": "lab",
     "profiles": {"lab": {"transport": "ssh", "ssh_host": "aether@lab"}}}

Profiles
    A profile is a named set of the same keys, so one machine can talk to
    several targets (a container here, a lab server there) without editing the
    repository. Resolution order for every setting is:

        environment variable  >  active profile  >  top-level config  >  default

    The active profile comes from ``PYAETHER_PROFILE``, else the nearest
    ``.pyaether-profile`` file (``pyaether profile bind <name>`` writes one in
    the current directory), else ``default_profile`` in the config file. The
    daemon socket is scoped per profile so two profiles never share a session.
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

# --- profiles -------------------------------------------------------------
PROFILE_BINDING_FILENAME = ".pyaether-profile"
PROFILE_SEARCH_DEPTH = 5  # how many parent directories to scan for the binding file


def _clean(value):
    text = str(value or "").strip()
    return text or None


def _read_binding(path):
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            name = _clean(line)
            if name and not name.startswith("#"):
                return name
    except OSError:
        return None
    return None


def find_binding(start=None):
    """Path of the nearest ``.pyaether-profile`` file, or None."""
    current = pathlib.Path(start or os.getcwd()).resolve()
    for _ in range(PROFILE_SEARCH_DEPTH + 1):
        candidate = current / PROFILE_BINDING_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def resolve_profile():
    """Return ``(name, source)`` for the active profile, or ``(None, reason)``."""
    from_env = _clean(os.environ.get("PYAETHER_PROFILE"))
    if from_env:
        return from_env, "PYAETHER_PROFILE"
    binding = find_binding()
    if binding is not None:
        name = _read_binding(binding)
        if name:
            return name, str(binding)
    default = _clean(USER_CONFIG.get("default_profile"))
    if default:
        return default, "default_profile in config.json"
    return None, "no profile bound"


PROFILE, PROFILE_SOURCE = resolve_profile()
PROFILE_SETTINGS = USER_CONFIG.get("profiles", {}).get(PROFILE, {}) if PROFILE else {}
if not isinstance(PROFILE_SETTINGS, dict):
    PROFILE_SETTINGS = {}


def known_profiles():
    """``{name: settings}`` for every profile defined in the config file."""
    profiles = USER_CONFIG.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {name: value for name, value in profiles.items() if isinstance(value, dict)}


def setting(env_name, key, default=None):
    """Resolution order: environment variable > active profile > config > default."""
    value = os.environ.get(env_name)
    if value:
        return value
    value = PROFILE_SETTINGS.get(key)
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

# --- simulators -----------------------------------------------------------
# A simulator does not have to run where PyAether runs (ngspice on a laptop,
# Spectre on a licensed server), so the simulator target resolves separately.
# An empty SIM_TARGET means "use the bridge transport".
SIM_TARGET = setting("PYAETHER_SIM_TARGET", "sim_target", "")
SIM_BACKEND = setting("PYAETHER_SIM_BACKEND", "sim_backend", "ngspice")
SIM_WORKDIR = setting("PYAETHER_SIM_WORKDIR", "sim_workdir", "/tmp/pyaether-sim")
SIM_TIMEOUT = setting("PYAETHER_SIM_TIMEOUT", "sim_timeout", "600")
SIM_SSH_HOST = setting("PYAETHER_SIM_SSH_HOST", "sim_ssh_host", "")
SIM_SSH_PORT = setting("PYAETHER_SIM_SSH_PORT", "sim_ssh_port", "")
SIM_SSH_OPTS = setting("PYAETHER_SIM_SSH_OPTS", "sim_ssh_opts", "")
SIM_CONTAINER = setting("PYAETHER_SIM_CONTAINER", "sim_container", "")
# Command template for the "custom" backend; empty means "not configured".
SIM_CMD = setting("PYAETHER_SIM_CMD", "sim_cmd", "")
# Extra environment for any simulator command, as "KEY=VALUE,KEY2=VALUE2".
# Needed when a tool writes binary output by default (ngspice wants
# SPICE_ASCIIRAWFILE=1 for a text rawfile) and no built-in backend sets it.
SIM_ENV = setting("PYAETHER_SIM_ENV", "sim_env", "")
NGSPICE_BIN = setting("PYAETHER_NGSPICE_BIN", "ngspice_bin", "ngspice")
SPECTRE_BIN = setting("PYAETHER_SPECTRE_BIN", "spectre_bin", "spectre")
SPECTRE_MODE = setting("PYAETHER_SPECTRE_MODE", "spectre_mode", "ax")
# Empyrean ALPS (the vendor simulator). Requires the user's own valid licence;
# this bridge never reads, alters or works around licensing.
ALPS_BIN = setting("PYAETHER_ALPS_BIN", "alps_bin", "alps")
ALPS_THREADS = setting("PYAETHER_ALPS_THREADS", "alps_threads", "")

# Each profile gets its own socket/pid/log: two profiles point at different
# targets, so sharing one daemon would hand out the wrong session.
RUNTIME_DIR = (DATA_DIR / "profiles" / PROFILE) if PROFILE else DATA_DIR
DAEMON_SOCK = _env_path("PYAETHER_DAEMON_SOCK") or (RUNTIME_DIR / "daemon.sock")
DAEMON_PID = RUNTIME_DIR / "daemon.pid"
DAEMON_LOG = RUNTIME_DIR / "daemon.log"

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
