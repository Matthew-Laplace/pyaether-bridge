#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek Harness integration probe: install the bridge into a profile.

Everything happens in a throwaway ``DSH_HOME`` with a minimal profile, so the
user's real harness configuration is never touched. The check that matters is
the last one: run the real ``dsh --profile <p> --dump-config`` and confirm the
MCP entry is in the *composed* tree. A patch file that merely exists on disk is
not evidence -- two real failure modes motivated this probe:

  * a bare ``- id:`` item is an id-targeted patch, so an entry for an id that
    does not exist yet is silently dropped;
  * an unquoted ``@scope/package`` name is invalid YAML, and the harness refuses
    to start.

Usage::

    python3 tests/dsh_probe.py

Skipped when ``dsh`` is not installed.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = []
FAILED = []
SKIPPED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       ("\n       %s" % detail) if (detail and not condition) else ""))


def skip(name, reason):
    SKIPPED.append(name)
    print("SKIP %s -- %s" % (name, reason))


def make_profile(home, name="probe"):
    """A minimal profile that composes on the shared core only."""
    profile = pathlib.Path(home) / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": "dsh-profile-%s" % name,
        "private": True,
        "dependencies": {},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
    }, indent=2) + "\n", encoding="utf-8")
    (profile / "cordis.yml").write_text("# root entry list; patches compose the tree\n[]\n",
                                        encoding="utf-8")
    (profile / "cordis.patch.yml").write_text(
        "# probe patch layer\n# a top-level YAML array of loader patch entries\n[]\n",
        encoding="utf-8")
    (profile / "pnpm-workspace.yaml").write_text(
        "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n", encoding="utf-8")
    return profile


def dump_config(home, profile, timeout=240):
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    proc = subprocess.run(["dsh", "--profile", profile, "--dump-config"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, text=True)
    return proc


def main():
    from pyaether_bridge import dsh

    print("== DeepSeek Harness integration probe ==")
    print("repo  : %s" % ROOT)
    binary = dsh.find_dsh()
    print("dsh   : %s" % (binary or "(not installed)"))
    print()
    if not binary:
        skip("dsh integration", "dsh is not on PATH; install DeepSeek Harness to "
                                "exercise this probe")
        return report()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyaether-dsh-"))
    make_profile(tmp)
    print("isolated DSH_HOME: %s" % tmp)
    print()

    # ---- 1. the profile composes before we touch it -----------------------
    print("-- 1. baseline --")
    before = dump_config(tmp, "probe")
    check("a minimal profile composes with dsh", before.returncode == 0,
          (before.stderr or "")[-400:])
    check("the baseline has no pyaether entry", "pyaether" not in before.stdout)
    print()

    # ---- 2. install -------------------------------------------------------
    print("-- 2. install --")
    report_install = dsh.install("probe", str(tmp))
    check("install wrote the entry", report_install["entry_written"] is True,
          json.dumps(report_install, ensure_ascii=False)[:300])
    check("install left a backup of the previous layer", bool(report_install["backup"]),
          str(report_install["backup"]))
    patch_text = (tmp / "profiles" / "probe" / "cordis.patch.yml").read_text(encoding="utf-8")
    check("the entry uses an insert block, not a bare id patch",
          "- insert:" in patch_text, patch_text[:300])
    check("the scoped package name is quoted",
          '"@deepseek-ai/dsh-mcp-client"' in patch_text or
          "'@deepseek-ai/dsh-mcp-client'" in patch_text, patch_text[:300])
    check("the tool-call timeout is raised above the 60s default",
          "toolCallTimeoutMs: %d" % dsh.DEFAULT_TOOL_TIMEOUT_MS in patch_text,
          patch_text[:400])
    skill = tmp / "skills" / dsh.SKILL_NAME / "SKILL.md"
    check("the skill was installed", skill.is_file(), str(skill))
    if skill.is_file():
        text = skill.read_text(encoding="utf-8")
        check("the skill has the frontmatter the provider requires",
              text.startswith("---") and "name:" in text and "description:" in text,
              text[:200])
    print()

    # ---- 3. the harness really composes it --------------------------------
    print("-- 3. composed by the harness (the check that matters) --")
    after = dump_config(tmp, "probe")
    check("dsh still composes the profile after the edit", after.returncode == 0,
          (after.stderr or "")[-500:])
    check("the composed tree contains the MCP entry",
          'id: pyaether-mcp' in after.stdout, after.stdout[-400:])
    check("the composed entry names the MCP client plugin",
          "dsh-mcp-client" in after.stdout)
    check("the composed entry points at bin/pyaether-mcp",
          "bin/pyaether-mcp" in after.stdout)

    verified = dsh.verify("probe", str(tmp))
    check("our own verify() agrees", verified.get("composed_ok") is True,
          json.dumps({k: verified.get(k) for k in ("composed_ok", "compose_error")},
                     ensure_ascii=False)[:400])
    print()

    # ---- 4. idempotency ---------------------------------------------------
    print("-- 4. installing twice --")
    dsh.install("probe", str(tmp))
    second = (tmp / "profiles" / "probe" / "cordis.patch.yml").read_text(encoding="utf-8")
    check("the entry is not duplicated",
          second.count('id: "pyaether-mcp"') + second.count("id: pyaether-mcp") == 1,
          "occurrences=%d" % (second.count('id: "pyaether-mcp"')
                              + second.count("id: pyaether-mcp")))
    still = dump_config(tmp, "probe")
    check("the profile still composes after a second install", still.returncode == 0,
          (still.stderr or "")[-300:])
    print()

    # ---- 5. uninstall -----------------------------------------------------
    print("-- 5. uninstall --")
    report_remove = dsh.uninstall("probe", str(tmp))
    check("uninstall removed the entry", report_remove["entry_removed"] is True,
          json.dumps(report_remove, ensure_ascii=False)[:300])
    check("uninstall removed the skill", report_remove["skill_removed"] is True)
    gone = dump_config(tmp, "probe")
    check("the profile still composes after uninstall", gone.returncode == 0,
          (gone.stderr or "")[-400:])
    check("no pyaether entry remains", "pyaether" not in gone.stdout)
    print()

    shutil.rmtree(tmp, ignore_errors=True)
    return report()


def report():
    print("-" * 64)
    print("result: %d PASS / %d FAIL / %d SKIP"
          % (len(PASSED), len(FAILED), len(SKIPPED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
