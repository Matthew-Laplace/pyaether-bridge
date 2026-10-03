# -*- coding: utf-8 -*-
"""DeepSeek Harness integration: use this bridge as a DSH plugin.

DeepSeek Harness (``dsh``) composes a profile from plugin bundles, and ships an
MCP client plugin (``@deepseek-ai/dsh-mcp-client``) that registers an external
MCP server's tools on the agent's tool list. This bridge already speaks MCP, so
"use it as a DSH plugin" means: add one entry to a profile that launches
``bin/pyaether-mcp``, plus the matching skill so the model knows how to drive it.

Two details that make the difference between "configured" and "works":

* ``toolCallTimeoutMs`` defaults to 60 s in the MCP client. A simulation or a
  layout job can run for minutes, so the entry raises it; without that, long
  calls fail as timeouts even though the tool is fine.
* The command must be an absolute path with the interpreter spelled out: the
  harness starts the server from its own working directory, and ``PYTHONPATH``
  is scrubbed for stdio servers.

Nothing here starts the harness or writes outside the paths the caller names.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from . import config

# The tools this bridge exposes; used for the patch and for status checks.
PLUGIN_ID = "pyaether-mcp"
SERVER_NAME = "pyaether"
SKILL_NAME = "pyaether-bridge"

# Generous default: a simulator or KLayout run may legitimately take minutes.
DEFAULT_TOOL_TIMEOUT_MS = 1800000  # 30 minutes


class DshError(RuntimeError):
    """dsh is missing, the profile layout is unexpected, or a write failed."""


def project_root():
    return str(config.PROJECT_DIR)


def mcp_command():
    """``(command, args)`` for the MCP server, absolute and interpreter-explicit."""
    root = project_root()
    server = os.path.join(root, "bin", "pyaether-mcp")
    interpreter = os.environ.get("PYAETHER_DSH_PYTHON") or shutil.which("python3") \
        or "/usr/bin/python3"
    return interpreter, [server]


def dsh_home():
    """Where dsh keeps profiles; ``$DSH_HOME`` wins, then ``~/.dsh``."""
    explicit = os.environ.get("DSH_HOME", "").strip()
    return os.path.abspath(explicit) if explicit else os.path.join(
        os.path.expanduser("~"), ".dsh")


def profile_dir(profile):
    return os.path.join(dsh_home(), "profiles", profile)


def patch_path(profile):
    return os.path.join(profile_dir(profile), "cordis.patch.yml")


def skills_dir():
    return os.path.join(dsh_home(), "skills")


def find_dsh():
    """Absolute path of the dsh CLI, or None."""
    return shutil.which("dsh")


def entry():
    """The MCP client entry as a dict (the YAML block is rendered from it)."""
    command, args = mcp_command()
    return {
        "id": PLUGIN_ID,
        "name": "@deepseek-ai/dsh-mcp-client",
        "config": {
            "serverName": SERVER_NAME,
            "transport": "stdio",
            "command": command,
            "args": args,
            "toolCallTimeoutMs": DEFAULT_TOOL_TIMEOUT_MS,
            "failOnStartupError": False,
        },
    }


def _yaml_scalar(value):
    """Render a scalar for the patch file.

    Strings are always quoted. Several characters that are common in real
    values -- a leading ``@`` in a scoped package name, ``#``, ``: ``, ``%`` --
    are YAML indicators, and an unquoted ``@deepseek-ai/dsh-mcp-client`` is a
    parse error (measured: the harness rejected the file with "end of the stream
    or a document separator is expected"). Booleans and numbers stay bare.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def render_entry():
    """Render the MCP entry as an ``insert`` item for ``cordis.patch.yml``.

    A new plugin is added with a top-level ``- insert:`` block holding the
    entries to append (this is the shape the harness documents for adding a tool
    to a base-backed profile). A bare ``- id:`` item is a *patch* aimed at an
    existing id, so an entry whose id does not exist yet is silently dropped --
    measured: the file parsed, the harness started, and the plugin never
    appeared in ``--dump-config``.

    Scalars are quoted: the package name starts with ``@``, a YAML indicator.
    """
    item = entry()
    lines = ["- insert:",
             "    - id: %s" % _yaml_scalar(item["id"]),
             "      name: %s" % _yaml_scalar(item["name"]),
             "      config:"]
    for key, value in item["config"].items():
        if isinstance(value, list):
            rendered = "[%s]" % ", ".join(_yaml_scalar(item) for item in value)
            lines.append("        %s: %s" % (key, rendered))
        else:
            lines.append("        %s: %s" % (key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n"


def render_patch():
    """A complete patch file body (header + entry) for a fresh profile layer."""
    command, _args = mcp_command()
    return (
        "# pyaether-bridge as a DeepSeek Harness plugin.\n"
        "# Written by `pyaether dsh install`; safe to keep in version control.\n"
        "# The MCP client plugin registers the bridge's tools as\n"
        "# mcp__%s__<tool> on the agent's tool list.\n"
        "\n" % SERVER_NAME
    ) + render_entry()


def _has_entry(text):
    """True when our entry id appears anywhere in the patch layer."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- id:"):
            continue
        value = stripped.split(":", 1)[1].strip().strip("'\"")
        if value == PLUGIN_ID:
            return True
    return False


def classify_patch(text):
    """How the existing patch layer can be extended.

    The harness parses this file as a list of loader patch entries, so the way
    we add one matters:

      * ``empty``       comments/whitespace only -> write a fresh list
      * ``empty-flow``  a bare ``[]`` document   -> replace it (appending block
                        items after ``[]`` is invalid YAML -- measured: the
                        harness rejects it with "end of the stream or a document
                        separator is expected")
      * ``block-list``  already a block sequence -> append another block item
                        (an ``- insert:`` block or an id-targeted patch)
      * ``other``       a flow sequence with items, or something else -> we do
                        not guess; the caller has to merge by hand
    """
    body = "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    stripped = body.strip()
    if not stripped:
        return "empty"
    if stripped in ("[]", "---\n[]"):
        return "empty-flow"
    if stripped.startswith("- ") or stripped == "-" or stripped.startswith("-\n"):
        return "block-list"
    return "other"


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _drop_entry(lines):
    """Remove our entry item from a patch list, keeping the rest intact."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- id:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value == PLUGIN_ID:
                start = index
                break
    if start is None:
        return lines
    indent = _indent_of(lines[start])
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].lstrip()
        if stripped.startswith("- ") and _indent_of(lines[index]) <= indent:
            end = index
            break
    return lines[:start] + lines[end:]


def _drop_empty_inserts(lines):
    """Remove ``- insert:`` blocks that no longer hold any entry."""
    out = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("- insert:") and _indent_of(line) == 0:
            end = len(lines)
            for scan in range(index + 1, len(lines)):
                if (lines[scan].lstrip().startswith("- ")
                        and _indent_of(lines[scan]) == 0):
                    end = scan
                    break
            block = lines[index:end]
            if any(item.lstrip().startswith("- id:") for item in block[1:]):
                out.extend(block)
            index = end
            continue
        out.append(line)
        index += 1
    return out


def status(profile="web", dsh_home_override=None):
    """Read-only view of whether the plugin is installed for a profile."""
    home = os.path.abspath(dsh_home_override) if dsh_home_override else dsh_home()
    profile_path = os.path.join(home, "profiles", profile)
    patch = os.path.join(profile_path, "cordis.patch.yml")
    text = ""
    if os.path.isfile(patch):
        try:
            with open(patch, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise DshError("cannot read %s: %s" % (patch, exc))
    skill = os.path.join(home, "skills", SKILL_NAME, "SKILL.md")
    return {
        "dsh_cli": find_dsh() or "",
        "dsh_home": home,
        "profile": profile,
        "profile_exists": os.path.isdir(profile_path),
        "patch_file": patch,
        "patch_exists": os.path.isfile(patch),
        "entry_installed": _has_entry(text),
        "skill_file": skill,
        "skill_installed": os.path.isfile(skill),
        "mcp_command": "%s %s" % mcp_command(),
        "tool_timeout_ms": DEFAULT_TOOL_TIMEOUT_MS,
    }


def install(profile="web", dsh_home_override=None, dry_run=False):
    """Add the MCP entry to a profile and install the skill.

    Appends to the profile's ``cordis.patch.yml`` (creating it if needed) and
    never rewrites existing content. Re-running is a no-op for the entry and
    refreshes the skill file.
    """
    home = os.path.abspath(dsh_home_override) if dsh_home_override else dsh_home()
    profile_path = os.path.join(home, "profiles", profile)
    patch = os.path.join(profile_path, "cordis.patch.yml")
    skill_source = os.path.join(project_root(), "integrations", "dsh", "skills",
                                SKILL_NAME, "SKILL.md")
    skill_target = os.path.join(home, "skills", SKILL_NAME, "SKILL.md")

    if not os.path.isdir(profile_path):
        raise DshError(
            "profile %r does not exist under %s; create it with "
            "`dsh <profile> --from-default-profile web` or pick another --profile"
            % (profile, home))
    if not os.path.isfile(skill_source):
        raise DshError("skill template is missing: %s" % skill_source)

    existing = ""
    if os.path.isfile(patch):
        try:
            with open(patch, encoding="utf-8") as handle:
                existing = handle.read()
        except OSError as exc:
            raise DshError("cannot read %s: %s" % (patch, exc))

    report = {
        "dsh_home": home,
        "profile": profile,
        "patch_file": patch,
        "skill_file": skill_target,
        "dry_run": bool(dry_run),
        "entry_written": False,
        "entry_already_present": _has_entry(existing),
        "backup": "",
        "skill_written": False,
        "rolled_back": False,
    }
    if dry_run:
        report["patch_preview"] = render_patch()
        return report

    shape = classify_patch(existing)
    report["patch_shape"] = shape
    if not report["entry_already_present"]:
        if shape in ("empty", "empty-flow"):
            body = render_patch()
        elif shape == "block-list":
            body = existing
            if not body.endswith("\n"):
                body += "\n"
            body += "\n" + (
                "# Added by `pyaether dsh install` -- pyaether-bridge MCP server.\n"
            ) + render_entry()
        else:
            raise DshError(
                "%s is not a plain patch list (it looks like an inline flow "
                "sequence), so appending would corrupt it. Add this entry by hand:\n\n%s"
                % (patch, render_entry()))
        if os.path.isfile(patch):
            backup = "%s.bak-%s" % (patch, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(patch, backup)
            report["backup"] = backup
        os.makedirs(os.path.dirname(patch), exist_ok=True)
        with open(patch, "w", encoding="utf-8") as handle:
            handle.write(body)
        report["entry_written"] = True

        # A file that parses in our hands may still be rejected by the harness.
        # Prove it composes; if it does not, put the previous file back so the
        # user is never left with a profile that cannot boot.
        if find_dsh():
            try:
                check = dump_config(profile, home)
            except DshError as exc:
                check = {"returncode": 1, "stderr": str(exc), "stdout": ""}
            report["compose_returncode"] = check["returncode"]
            if check["returncode"] != 0:
                if report["backup"]:
                    shutil.copy2(report["backup"], patch)
                else:
                    os.unlink(patch)
                report["entry_written"] = False
                report["rolled_back"] = True
                raise DshError(
                    "the harness rejected the edited patch file, so the change was "
                    "rolled back (%s). Harness output:\n%s"
                    % (patch, (check.get("stderr") or "").strip()[-1200:]))

    os.makedirs(os.path.dirname(skill_target), exist_ok=True)
    shutil.copy2(skill_source, skill_target)
    report["skill_written"] = True
    return report


def uninstall(profile="web", dsh_home_override=None):
    """Remove the entry from the profile patch and delete the installed skill."""
    home = os.path.abspath(dsh_home_override) if dsh_home_override else dsh_home()
    patch = os.path.join(home, "profiles", profile, "cordis.patch.yml")
    skill_target = os.path.join(home, "skills", SKILL_NAME, "SKILL.md")
    report = {"patch_file": patch, "skill_file": skill_target,
              "entry_removed": False, "skill_removed": False, "backup": ""}
    if os.path.isfile(patch):
        with open(patch, encoding="utf-8") as handle:
            text = handle.read()
        if _has_entry(text):
            lines = text.splitlines(keepends=True)
            lines = _drop_entry(lines)
            # If the insert block we lived in has no items left, drop it too,
            # otherwise an empty `- insert:` would be written back to the user.
            lines = _drop_empty_inserts(lines)
            # A file left with comments only parses as null, not as a list, and
            # the harness refuses it (measured: `--dump-config` exits 1). Keep a
            # valid empty list in that case.
            if not any(line.strip() and not line.lstrip().startswith("#")
                       for line in lines):
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append("[]\n")
            if len(lines) != len(text.splitlines(keepends=True)):
                backup = "%s.bak-%s" % (patch, time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(patch, backup)
                report["backup"] = backup
                with open(patch, "w", encoding="utf-8") as handle:
                    handle.write("".join(lines))
                report["entry_removed"] = True
    if os.path.isfile(skill_target):
        os.unlink(skill_target)
        report["skill_removed"] = True
        parent = os.path.dirname(skill_target)
        try:
            os.rmdir(parent)
        except OSError:
            pass
    return report


def dump_config(profile="web", dsh_home_override=None, timeout=180.0):
    """Run ``dsh --profile <p> --dump-config`` read-only and return its output.

    This is the only check that proves the harness actually composed the entry:
    a patch file on disk can still be overridden or mis-parsed.
    """
    binary = find_dsh()
    if not binary:
        raise DshError("dsh is not on PATH; install DeepSeek Harness first")
    env = dict(os.environ)
    if dsh_home_override:
        env["DSH_HOME"] = os.path.abspath(dsh_home_override)
    try:
        proc = subprocess.run([binary, "--profile", profile, "--dump-config"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DshError("`dsh --dump-config` timed out after %.0fs" % timeout)
    except OSError as exc:
        raise DshError("could not run dsh: %s" % exc)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace")[-2000:],
        "dsh": binary,
    }


def verify(profile="web", dsh_home_override=None):
    """Install-state check: does the composed config really contain our entry?"""
    state = status(profile, dsh_home_override)
    result = dict(state)
    result["composed_ok"] = False
    result["composed"] = ""
    result["compose_error"] = ""
    if not state["dsh_cli"]:
        result["compose_error"] = "dsh is not on PATH"
        return result
    try:
        dumped = dump_config(profile, dsh_home_override)
    except DshError as exc:
        result["compose_error"] = str(exc)
        return result
    result["composed"] = dumped["stdout"]
    result["dsh_returncode"] = dumped["returncode"]
    if dumped["returncode"] != 0:
        result["compose_error"] = dumped["stderr"] or "dsh --dump-config failed"
        return result
    text = dumped["stdout"]
    result["composed_ok"] = (
        ("id: %s" % PLUGIN_ID) in text
        and "dsh-mcp-client" in text
        and ("serverName: %s" % SERVER_NAME) in text
    )
    if not result["composed_ok"]:
        result["compose_error"] = ("the composed config does not contain the "
                                   "pyaether MCP entry")
    return result
