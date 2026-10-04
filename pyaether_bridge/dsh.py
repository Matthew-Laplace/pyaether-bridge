# -*- coding: utf-8 -*-
"""DeepSeek Harness integration: the workspace skill, and the bundle wiring.

DeepSeek Harness (``dsh``) composes a profile from plugin bundles and ships an
MCP client plugin (``@deepseek-ai/dsh-mcp-client``) that registers an external
MCP server's tools on the agent's tool list. This bridge already speaks MCP, so
registering it is a *bundle*: a small local package whose ``cordis.patch.yml``
inserts one MCP client row. That bundle lives beside this repository
(``<workspace>/dsh-bundle-pyaether-bridge``) and is installed through the
harness' own plugin manager -- the only channel that works for a profile the
desktop application manages.

This module deliberately does **not** edit a profile's ``cordis.patch.yml``:

* A profile the Electron application manages refuses CLI composition (measured:
  ``error: profile "desktop" is managed exclusively by the Electron
  application``), so an edit written here can never be verified and has to be
  rolled back. Shipping an installer that always rolls back is worse than
  shipping none.
* The bundle channel does the same job without touching the profile by hand.

What this module owns is the **skill**. The filesystem skill provider ships
disabled, and once enabled it scans an explicit ``customSkillDirs`` list, so the
global ``$DSH_HOME/skills`` directory is never read -- a skill written there is
installed and invisible. The skill therefore goes to
``<workspace>/.dsh/skills/pyaether-bridge/SKILL.md``, and ``status`` reports
whether a profile actually scans that root, because installed-but-unscanned
changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil

from . import config

# The bundle that registers this bridge, and the row/mcp server names it uses.
BUNDLE_NAME = "dsh-bundle-pyaether-bridge"
MCP_ROW_ID = "mcp-pyaether-bridge"
SERVER_NAME = "pyaether"
SKILL_NAME = "pyaether-bridge"

# The marker the harness' instruction loader also treats as the workspace root,
# so skill placement and instruction lookup agree on what "the workspace" is.
WORKSPACE_MARKER = ".dsh-workspace"

# The provider row that decides whether ``customSkillDirs`` is read at all.
SKILL_PROVIDER_ROW = "skill-filesystem"


class DshError(RuntimeError):
    """The workspace layout is unexpected, or a write failed."""


def project_root():
    return str(config.PROJECT_DIR)


def dsh_home():
    """Where dsh keeps profiles; ``$DSH_HOME`` wins, then ``~/.dsh``."""
    explicit = os.environ.get("DSH_HOME", "").strip()
    return os.path.abspath(explicit) if explicit else os.path.join(
        os.path.expanduser("~"), ".dsh")


def _resolve_home(dsh_home_override):
    return (os.path.abspath(dsh_home_override) if dsh_home_override
            else dsh_home())


def find_workspace(explicit=None, start=None):
    """The workspace root holding ``.dsh/skills``.

    Order: an explicit ``--workspace``, then ``$DSH_WORKSPACE``, then the nearest
    ancestor of ``start`` (default: the current directory) carrying a
    ``.dsh-workspace`` marker, and finally ``start`` itself. The marker is what
    the harness' instruction loader pins too, so a session started deep inside a
    project still resolves the same root.
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    from_env = os.environ.get("DSH_WORKSPACE", "").strip()
    if from_env:
        return os.path.abspath(os.path.expanduser(from_env))
    origin = os.path.abspath(start or os.getcwd())
    current = origin
    while True:
        if os.path.isfile(os.path.join(current, WORKSPACE_MARKER)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return origin
        current = parent


def skills_root(workspace):
    return os.path.join(workspace, ".dsh", "skills")


def skill_source():
    return os.path.join(project_root(), "integrations", "dsh", "skills",
                        SKILL_NAME, "SKILL.md")


def skill_target(workspace):
    return os.path.join(skills_root(workspace), SKILL_NAME, "SKILL.md")


def bundle_dir(workspace):
    return os.path.join(workspace, BUNDLE_NAME)


def profiles(dsh_home_override=None):
    """Every profile directory under the harness home, sorted."""
    root = os.path.join(_resolve_home(dsh_home_override), "profiles")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(name for name in names
                  if os.path.isdir(os.path.join(root, name)))


def _row_body(text, row_id):
    """The lines belonging to the top-level ``- id: <row_id>`` row, or ``""``.

    The patch layer is a YAML list of loader patch entries, so a row ends at the
    next line that starts a new top-level item. Good enough to read back the two
    facts this module needs (a path is listed, a row is disabled) without pulling
    in a YAML parser -- the project is standard-library only.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- id:"):
            continue
        if stripped.split(":", 1)[1].strip().strip("'\"") == row_id:
            start = index
            break
    if start is None:
        return ""
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("-"):
            break
        body.append(line)
    return "\n".join(body)


def _row_disabled(body):
    for line in body.splitlines()[1:]:
        if line.strip() == "disabled: true":
            return True
    return False


def skill_root_state(workspace, dsh_home_override=None):
    """Which profiles scan this skill root, and which have the provider disabled.

    Both facts matter: ``customSkillDirs`` is only read while the provider row is
    enabled, so a profile can list the root and still load nothing.
    """
    home = _resolve_home(dsh_home_override)
    root = skills_root(workspace)
    enabled, disabled = [], []
    for name in profiles(dsh_home_override):
        patch = os.path.join(home, "profiles", name, "cordis.patch.yml")
        try:
            with open(patch, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        body = _row_body(text, SKILL_PROVIDER_ROW)
        if not body or root not in body:
            continue
        (disabled if _row_disabled(body) else enabled).append(name)
    return {"skills_root": root, "enabled_in": enabled, "disabled_in": disabled}


def bundle_state(workspace, dsh_home_override=None):
    """Is the local bundle there, and which profiles declare it?"""
    directory = bundle_dir(workspace)
    manifest = os.path.join(directory, "package.json")
    present = os.path.isfile(manifest)
    linked = []
    if present:
        home = _resolve_home(dsh_home_override)
        for name in profiles(dsh_home_override):
            profile_manifest = os.path.join(home, "profiles", name, "package.json")
            try:
                with open(profile_manifest, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            profile = (data.get("dsh") or {}).get("profile") or {}
            if (BUNDLE_NAME in (data.get("dependencies") or {})
                    or BUNDLE_NAME in (profile.get("bundles") or [])):
                linked.append(name)
    return {"bundle_dir": directory, "bundle_present": present,
            "bundle_linked_in": linked}


def _files_match(left, right):
    try:
        with open(left, "rb") as first, open(right, "rb") as second:
            return first.read() == second.read()
    except OSError:
        return False


def status(workspace=None, dsh_home_override=None):
    """Read-only view of the harness wiring: skill, its scan root, and the bundle."""
    workspace = find_workspace(workspace)
    source = skill_source()
    target = skill_target(workspace)
    installed = os.path.isfile(target)
    report = {
        "dsh_home": _resolve_home(dsh_home_override),
        "workspace": workspace,
        "workspace_marker": os.path.isfile(os.path.join(workspace, WORKSPACE_MARKER)),
        "skill_source": source,
        "skill_file": target,
        "skill_installed": installed,
        "skill_in_sync": bool(installed) and _files_match(source, target),
        "server_name": SERVER_NAME,
        "bundle_name": BUNDLE_NAME,
    }
    root_state = skill_root_state(workspace, dsh_home_override)
    report["skills_root"] = root_state["skills_root"]
    report["skill_root_enabled_in"] = root_state["enabled_in"]
    report["skill_root_disabled_in"] = root_state["disabled_in"]
    report["skill_root_scanned"] = bool(root_state["enabled_in"])
    report.update(bundle_state(workspace, dsh_home_override))
    return report


def install(workspace=None, dsh_home_override=None, dry_run=False):
    """Copy the skill into ``<workspace>/.dsh/skills`` -- the only place it loads.

    Registration of ``mcp__pyaether__*`` is the bundle's job, done with the
    harness' own plugin manager; nothing here writes to a profile.
    """
    workspace = find_workspace(workspace)
    source = skill_source()
    target = skill_target(workspace)
    if not os.path.isfile(source):
        raise DshError("skill template is missing: %s" % source)
    root_state = skill_root_state(workspace, dsh_home_override)
    report = {
        "workspace": workspace,
        "skill_source": source,
        "skill_file": target,
        "skills_root": root_state["skills_root"],
        "skill_root_enabled_in": root_state["enabled_in"],
        "skill_root_disabled_in": root_state["disabled_in"],
        "dry_run": bool(dry_run),
        "skill_written": False,
        "unchanged": False,
        "next": "",
    }
    if dry_run:
        return report
    if os.path.isfile(target) and _files_match(source, target):
        report["unchanged"] = True
        return report
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)
    report["skill_written"] = True
    report["next"] = next_step(root_state, dsh_home_override)
    return report


def next_step(root_state, dsh_home_override=None):
    """What still has to be true outside this repository for the skill to load."""
    if root_state["enabled_in"]:
        return ("skill root already scanned by profile(s): %s"
                % ", ".join(root_state["enabled_in"]))
    return ("no profile scans this skill root yet, so the skill is installed but "
            "never loaded. Add it to the %s row of a profile patch under %s:\n"
            "  customSkillDirs:\n    - \"%s\"\n"
            "and keep that row enabled (`disabled: false`)."
            % (SKILL_PROVIDER_ROW, os.path.join(_resolve_home(dsh_home_override),
                                                "profiles"),
               root_state["skills_root"]))


def uninstall(workspace=None, dsh_home_override=None):
    """Remove the workspace skill; the bundle stays registered in its profile."""
    workspace = find_workspace(workspace)
    target = skill_target(workspace)
    report = {"workspace": workspace, "skill_file": target, "skill_removed": False}
    if os.path.isfile(target):
        os.unlink(target)
        report["skill_removed"] = True
        for directory in (os.path.dirname(target), skills_root(workspace)):
            try:
                os.rmdir(directory)
            except OSError:
                break
    return report
