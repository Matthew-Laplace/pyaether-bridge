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
    PYAETHER_AETHER_VERSION  the Aether release this profile targets; pins the
                             symbol catalog to that release
    PYAETHER_REQUIRE_EXPLICIT_PROFILE
                             refuse to act on an inherited profile: only
                             PYAETHER_PROFILE in this process counts as naming
                             the target
    PYAETHER_ALLOW_IDENTITY_MISMATCH
                             accept a target that does not match the expected_*
                             values instead of refusing
    PYAETHER_ALLOW_STALE_DAEMON
                             accept a running daemon that serves a different
                             target than this process resolved
    PYAETHER_EXPECTED_HOSTNAME / _CONTAINER / _IMAGE / _AETHER_VERSION /
    _LICENSE_SERVER          what the target is asserted to be; a declared value
                             that cannot be verified is a failure, not a pass

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

import hashlib
import json
import os
import pathlib
import re

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


def flag(env_name, key, default=False):
    """Boolean setting with the same resolution order as ``setting``."""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = PROFILE_SETTINGS.get(key)
    if raw is None:
        raw = USER_CONFIG.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


def profile_is_confirmed():
    """True when *this* process named the profile, rather than inheriting one.

    Only ``PYAETHER_PROFILE`` counts. A binding file or a ``default_profile``
    selects a target, but selection is not confirmation for the current command,
    so neither satisfies the explicit-profile guard. (Same distinction the
    upstream Virtuoso bridge draws for ``VB_REQUIRE_EXPLICIT_PROFILE``.)
    """
    return PROFILE_SOURCE == "PYAETHER_PROFILE"


def scoped_path(base, profile=None):
    """Namespace a target-side path by the active profile.

    ``/tmp/pyaether-bridge`` becomes ``/tmp/pyaether-bridge-<profile>``. Two
    profiles can point at the same host, and a shared directory lets one
    overwrite the other's deployed session script or staged netlist. With no
    active profile the path is returned unchanged, so single-target setups keep
    their existing paths.
    """
    name = PROFILE if profile is None else profile
    if not name:
        return str(base)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name).strip())[:64] or "profile"
    return "%s-%s" % (str(base).rstrip("/"), safe)


class ConfigError(RuntimeError):
    """A setting is missing or ambiguous in a way that would silently pick wrong."""


TRANSPORT = setting("PYAETHER_TRANSPORT", "transport", "docker")
CONTAINER = setting("PYAETHER_CONTAINER", "container", "empyrean-gui")
SSH_HOST = setting("PYAETHER_SSH_HOST", "ssh_host", "")
SSH_PORT = setting("PYAETHER_SSH_PORT", "ssh_port", "")
SSH_OPTS = setting("PYAETHER_SSH_OPTS", "ssh_opts", "")
PYTHON = setting("PYAETHER_PYTHON", "python", "python3.9")
# Namespaced by profile: two profiles can share one host, and a fixed
# /tmp/pyaether-bridge/session_bridge.py would let one profile overwrite the
# other's deployed session script (a second bridge version would clobber it).
REMOTE_DIR = setting("PYAETHER_REMOTE_DIR", "remote_dir") or scoped_path("/tmp/pyaether-bridge")
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
SIM_WORKDIR = setting("PYAETHER_SIM_WORKDIR", "sim_workdir") or scoped_path("/tmp/pyaether-sim")
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

# --- layout (KLayout) -----------------------------------------------------
# KLayout is open source and usually not part of the EDA installation, so like
# the simulator target it defaults to this machine when the binary is here.
KLAYOUT_BIN = setting("PYAETHER_KLAYOUT_BIN", "klayout_bin", "klayout")
KLAYOUT_TARGET = setting("PYAETHER_KLAYOUT_TARGET", "klayout_target", "")
# KLayout also ships standalone stream tools (strm2oas, strm2gds, strmcmp,
# strmclip, strmxor, ...). They are not on PATH -- on macOS they live in
# KLayout.app/Contents/Buddy -- so the directory is configurable and empty means
# "look next to the klayout binary".
KLAYOUT_BUDDY_DIR = setting("PYAETHER_KLAYOUT_BUDDY_DIR", "klayout_buddy_dir", "")
LAYOUT_WORKDIR = setting("PYAETHER_LAYOUT_WORKDIR", "layout_workdir") or scoped_path("/tmp/pyaether-layout")
LAYOUT_TIMEOUT = setting("PYAETHER_LAYOUT_TIMEOUT", "layout_timeout", "600")

# --- schematic round trip -------------------------------------------------
# Where generated schematics are staged on the target, and how the target's
# library definitions are found (aether's lib.defs registers the libraries; a
# library that is not registered cannot be opened).
SCHEMATIC_WORKDIR = setting("PYAETHER_SCH_WORKDIR", "sch_workdir") or scoped_path("/tmp/pyaether-sch")
SCHEMATIC_LIBDEFS = setting("PYAETHER_SCH_LIBDEFS", "sch_libdefs", "")
SCHEMATIC_AETHER_ROOT = setting("PYAETHER_SCH_AETHER_ROOT", "sch_aether_root", "")

# Each profile gets its own socket/pid/log: two profiles point at different
# targets, so sharing one daemon would hand out the wrong session.
RUNTIME_DIR = (DATA_DIR / "profiles" / PROFILE) if PROFILE else DATA_DIR
DAEMON_SOCK = _env_path("PYAETHER_DAEMON_SOCK") or (RUNTIME_DIR / "daemon.sock")
DAEMON_PID = RUNTIME_DIR / "daemon.pid"
DAEMON_LOG = RUNTIME_DIR / "daemon.log"
# The startup lock also lives per profile: a single shared lock let the first
# profile's daemon block every other profile's daemon from starting, even though
# each profile has its own socket, session and target.
DAEMON_LOCK = RUNTIME_DIR / "daemon.lock"

# --- symbol catalog -------------------------------------------------------
# The catalog is derived from one installation's Sphinx docs, which makes it a
# version-dependent artifact: a single shared database answers cross-version
# queries with signatures that may not exist in the target's release.
AETHER_VERSION = setting("PYAETHER_AETHER_VERSION", "aether_version")


def catalog_db_path():
    """Catalog for the active profile, keyed by the declared Aether version.

    Resolution: ``PYAETHER_CATALOG_DB`` > profile/config ``catalog_db`` > a
    per-(profile, version) file under the data dir > the historical single
    catalog (used when no version is declared, so existing setups are unchanged).
    """
    override = _env_path("PYAETHER_CATALOG_DB")
    if override:
        return override
    configured = setting("PYAETHER_CATALOG_DB", "catalog_db")
    if configured:
        return pathlib.Path(str(configured)).expanduser()
    if AETHER_VERSION:
        return (DATA_DIR / "profiles" / (PROFILE or "default")
                / ("catalog-%s.sqlite" % AETHER_VERSION))
    return PROJECT_DIR / "data" / "catalog.sqlite"


CATALOG_DB = catalog_db_path()

# `api build` only needs the docs that ship inside an installed Aether tree.
_DOCS_TAIL = pathlib.Path("tools/pyaether/docs/html")
_INSTALL_ROOTS = (pathlib.Path("/opt/empyrean"), pathlib.Path.home() / "empyrean")

NO_AUTOSTART = os.environ.get("PYAETHER_BRIDGE_NO_AUTOSTART", "") not in ("", "0", "false")


def _is_docs_dir(path):
    if not path:
        return False
    candidate = pathlib.Path(path)
    return candidate.is_dir() and (candidate / "objects.inv").exists()


def docs_pin():
    """The explicitly configured docs directory, or None when discovery decides.

    Reads the environment, then the active profile, then the config file -- a
    ``docs_dir`` inside a profile block used to be ignored, which silently made
    every profile build from the same installation's docs.
    """
    value = setting("PYAETHER_DOCS_DIR", "docs_dir")
    if value:
        return pathlib.Path(str(value)).expanduser()
    aether_root = setting("PYAETHER_AETHER_ROOT", "aether_root")
    if aether_root:
        return pathlib.Path(str(aether_root)).expanduser() / _DOCS_TAIL
    return None


def installed_docs():
    """``[(install name, docs/html path)]`` for every Aether tree on this host."""
    found = []
    for base in _INSTALL_ROOTS:
        try:
            entries = sorted(base.glob(str(pathlib.Path("*") / _DOCS_TAIL)))
        except OSError:
            continue
        for path in entries:
            name = path.parts[-4] if len(path.parts) >= 4 else str(path)
            found.append((name, path))
    return found


def resolve_docs_dir(explicit=None):
    """The docs directory to build the catalog from, refusing to guess.

    Several installed Aether releases make discovery ambiguous, and picking one
    arbitrarily is how a catalog ends up describing the wrong release. A pinned
    value is used as-is (and must be usable); otherwise exactly one discovered
    installation is required.
    """
    pinned = explicit or docs_pin()
    if pinned:
        path = pathlib.Path(str(pinned)).expanduser()
        if _is_docs_dir(path):
            return path
        raise ConfigError(
            "the configured docs directory is not a Sphinx html directory "
            "(no objects.inv): %s" % path)
    usable = [(name, path) for name, path in installed_docs() if _is_docs_dir(path)]
    if len(usable) == 1:
        return usable[0][1]
    if not usable:
        legacy = DATA_DIR / "docs" / "html"
        return legacy if _is_docs_dir(legacy) else None
    raise ConfigError(
        "%d Aether installations are present, so the docs directory is "
        "ambiguous: %s. Pin one per profile with \"docs_dir\", or set "
        "PYAETHER_DOCS_DIR / PYAETHER_AETHER_ROOT."
        % (len(usable), ", ".join(name for name, _ in usable)))


def find_docs_dir():
    """Best-effort docs directory (first usable candidate), or None.

    Kept for callers that only want a hint; ``api build`` uses
    :func:`resolve_docs_dir`, so an ambiguous host is an error, not a coin flip.
    """
    pinned = docs_pin()
    if _is_docs_dir(pinned):
        return pathlib.Path(str(pinned)).expanduser()
    for _name, path in installed_docs():
        if _is_docs_dir(path):
            return path
    legacy = DATA_DIR / "docs" / "html"
    return legacy if _is_docs_dir(legacy) else None


# --- target identity ------------------------------------------------------
# A profile names a target; these values assert which target it actually is.
# Declaring one is a claim, so a value that cannot be verified counts as a
# failure: silently passing an unverifiable assertion is the "wrong server"
# failure mode this exists to prevent. Override with
# PYAETHER_ALLOW_IDENTITY_MISMATCH when a mismatch is intentional.
IDENTITY_FIELDS = ("hostname", "container", "image", "aether_version", "license_server")

EXPECTED_IDENTITY = {
    field: setting("PYAETHER_EXPECTED_%s" % field.upper(), "expected_%s" % field)
    for field in IDENTITY_FIELDS
}
EXPECTED_IDENTITY_ACTIVE = any(EXPECTED_IDENTITY.values())

REQUIRE_EXPLICIT_PROFILE = flag("PYAETHER_REQUIRE_EXPLICIT_PROFILE",
                                "require_explicit_profile")
ALLOW_IDENTITY_MISMATCH = flag("PYAETHER_ALLOW_IDENTITY_MISMATCH",
                               "allow_identity_mismatch")
ALLOW_STALE_DAEMON = flag("PYAETHER_ALLOW_STALE_DAEMON", "allow_stale_daemon")


def profile_confirmation_error(operation):
    """Refuse to act on an inherited profile while the guard is on; None if fine."""
    if not REQUIRE_EXPLICIT_PROFILE or profile_is_confirmed():
        return None
    return (
        "PYAETHER_REQUIRE_EXPLICIT_PROFILE is set, so %s must name its target. "
        "The active profile is %r, selected by %s -- that selects a target but is "
        "not a confirmation for this command. Export PYAETHER_PROFILE=<name>, or "
        "unset the guard." % (operation, PROFILE, PROFILE_SOURCE))


def target_fingerprint():
    """Fingerprint of the target a daemon serves.

    The daemon socket is keyed by profile already, so a mismatch here means the
    profile's *target* changed while a daemon kept running: reusing that session
    would quietly act on the old target.
    """
    material = json.dumps({
        "transport": TRANSPORT,
        "container": CONTAINER,
        "ssh_host": SSH_HOST,
        "ssh_port": str(SSH_PORT or ""),
        "python": PYTHON,
        "remote_dir": REMOTE_DIR,
        "license_server": LICENSE_SERVER or "",
        "aether_version": AETHER_VERSION or "",
        "sim_target": SIM_TARGET or "",
        "sim_container": SIM_CONTAINER or "",
        "sim_ssh_host": SIM_SSH_HOST or "",
        "klayout_target": KLAYOUT_TARGET or "",
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


TARGET_FINGERPRINT = target_fingerprint()
