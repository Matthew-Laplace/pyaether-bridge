#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek Harness integration probe: workspace skill placement and wiring.

The bridge is registered with the harness by a *bundle* (a local package whose
patch inserts one MCP client row), which the harness' own plugin manager
installs. What this module owns is the skill, and the fact that matters is not
"was a file written" but "will the harness ever read it": the filesystem skill
provider ships disabled, and once enabled it scans an explicit
``customSkillDirs`` list, so a skill under the global ``$DSH_HOME/skills`` is
installed and invisible.

Everything happens in a throwaway workspace and a throwaway ``DSH_HOME``. The
probe asserts the guarantee that replaced the old profile-patch installer: no
profile file is written to at all.

Usage::

    python3 tests/dsh_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
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


def make_workspace(root):
    workspace = pathlib.Path(root) / "workspace"
    (workspace / "project" / "deep").mkdir(parents=True)
    (workspace / ".dsh-workspace").write_text("probe workspace marker\n", encoding="utf-8")
    return workspace


def make_profile(home, name, skills_root=None, disabled=False):
    """A profile whose skill-filesystem row scans (or refuses) one skills root."""
    profile = pathlib.Path(home) / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": "dsh-profile-%s" % name,
        "private": True,
        "dependencies": {},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
    }, indent=2) + "\n", encoding="utf-8")
    (profile / "cordis.yml").write_text("[]\n", encoding="utf-8")
    lines = ["# probe patch layer",
             "- id: skill-filesystem",
             '  name: "@deepseek-ai/dsh-skill-filesystem"',
             "  disabled: %s" % ("true" if disabled else "false"),
             "  config:",
             "    customSkillDirs:"]
    if skills_root:
        lines.append('      - "%s"' % skills_root)
    lines += ["- id: llm-deepseek", "  config:", "    models: []"]
    (profile / "cordis.patch.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile


def fingerprint(profile):
    """Every byte of a profile, so "we did not touch it" is checkable."""
    parts = []
    for path in sorted(pathlib.Path(profile).rglob("*")):
        if path.is_file():
            parts.append((str(path.relative_to(profile)), path.read_bytes()))
    return parts


def main():
    from pyaether_bridge import dsh

    print("== DeepSeek Harness integration probe ==")
    print("repo: %s" % ROOT)
    print()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyaether-dsh-"))
    workspace = make_workspace(tmp)
    home = tmp / "dsh-home"
    root = dsh.skills_root(str(workspace))
    try:
        # ---- 1. workspace resolution -------------------------------------
        print("-- 1. workspace root --")
        marker_found = dsh.find_workspace(start=str(workspace / "project" / "deep"))
        check("the .dsh-workspace marker is found from a nested directory",
              marker_found == str(workspace), "got %s" % marker_found)
        check("an explicit --workspace wins",
              dsh.find_workspace(explicit=str(tmp)) == str(tmp))
        plain = tmp / "no-marker"
        plain.mkdir()
        check("without a marker the start directory is used as-is",
              dsh.find_workspace(start=str(plain)) == str(plain))
        print()

        # ---- 2. install ---------------------------------------------------
        print("-- 2. install the skill --")
        alpha = make_profile(home, "alpha", skills_root=root, disabled=False)
        beta = make_profile(home, "beta", skills_root=root, disabled=True)
        before = {name: fingerprint(tmp / "dsh-home" / "profiles" / name)
                  for name in ("alpha", "beta")}

        report = dsh.install(str(workspace), str(home))
        target = pathlib.Path(dsh.skill_target(str(workspace)))
        check("install wrote the skill into the workspace",
              target.is_file(), str(target))
        check("the skill landed under <workspace>/.dsh/skills",
              str(target).startswith(str(workspace / ".dsh" / "skills")),
              str(target))
        check("the installed skill is the repository template",
              target.read_bytes() == pathlib.Path(dsh.skill_source()).read_bytes())
        text = target.read_text(encoding="utf-8")
        check("the skill has the frontmatter the provider requires",
              text.startswith("---") and "name:" in text and "description:" in text,
              text[:120])
        check("install reports which profile scans the root",
              report["skill_root_enabled_in"] == ["alpha"],
              json.dumps(report["skill_root_enabled_in"]))
        check("install notices the disabled provider row",
              report["skill_root_disabled_in"] == ["beta"],
              json.dumps(report["skill_root_disabled_in"]))
        after = {name: fingerprint(tmp / "dsh-home" / "profiles" / name)
                 for name in ("alpha", "beta")}
        check("install wrote nothing into any profile", before == after,
              "a profile file changed")
        print()

        # ---- 3. status ----------------------------------------------------
        print("-- 3. status --")
        state = dsh.status(str(workspace), str(home))
        check("status sees the skill installed", state["skill_installed"] is True)
        check("status sees the installed copy in sync with the repo",
              state["skill_in_sync"] is True)
        check("status reports the root as scanned", state["skill_root_scanned"] is True)
        check("status names the scanning profile",
              state["skill_root_enabled_in"] == ["alpha"])
        check("status names the profile with the provider disabled",
              state["skill_root_disabled_in"] == ["beta"])
        check("status resolves the workspace marker", state["workspace_marker"] is True)
        print()

        # ---- 4. dry run and idempotency -----------------------------------
        print("-- 4. dry run, then install twice --")
        dry = dsh.install(str(workspace), str(home), dry_run=True)
        check("dry run reports what it would do without claiming a write",
              dry["dry_run"] is True and dry["skill_written"] is False)
        again = dsh.install(str(workspace), str(home))
        check("a second install reports the copy already up to date",
              again["unchanged"] is True and again["skill_written"] is False)
        print()

        # ---- 5. an unscanned workspace tells you why ----------------------
        print("-- 5. a workspace no profile scans --")
        lonely = tmp / "lonely"
        (lonely / ".dsh").mkdir(parents=True)
        (lonely / ".dsh-workspace").write_text("marker\n", encoding="utf-8")
        lonely_report = dsh.install(str(lonely), str(home))
        check("install still places the skill",
              pathlib.Path(dsh.skill_target(str(lonely))).is_file())
        check("the report says the skill will not be loaded",
              "no profile scans this skill root" in lonely_report["next"],
              lonely_report["next"])
        check("the report carries the exact line to add",
              "customSkillDirs" in lonely_report["next"]
              and dsh.skills_root(str(lonely)) in lonely_report["next"],
              lonely_report["next"])
        print()

        # ---- 6. bundle wiring ---------------------------------------------
        print("-- 6. bundle wiring --")
        bundle = pathlib.Path(dsh.bundle_dir(str(workspace)))
        state = dsh.bundle_state(str(workspace), str(home))
        check("a missing bundle is reported as missing",
              state["bundle_present"] is False and state["bundle_linked_in"] == [],
              json.dumps(state))
        bundle.mkdir(parents=True)
        (bundle / "package.json").write_text(json.dumps({
            "name": dsh.BUNDLE_NAME, "version": "0.1.0", "private": True,
        }) + "\n", encoding="utf-8")
        manifest = json.loads((alpha / "package.json").read_text(encoding="utf-8"))
        manifest["dependencies"][dsh.BUNDLE_NAME] = "link:%s" % bundle
        (alpha / "package.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                            encoding="utf-8")
        state = dsh.bundle_state(str(workspace), str(home))
        check("a present bundle is reported present", state["bundle_present"] is True)
        check("the profile that declares the bundle is named",
              state["bundle_linked_in"] == ["alpha"],
              json.dumps(state["bundle_linked_in"]))
        print()

        # ---- 7. uninstall --------------------------------------------------
        print("-- 7. uninstall --")
        removed = dsh.uninstall(str(workspace), str(home))
        check("uninstall removed the skill", removed["skill_removed"] is True)
        check("uninstall left no empty .dsh/skills tree",
              not (workspace / ".dsh" / "skills").exists(),
              str(workspace / ".dsh" / "skills"))
        check("uninstall keeps the workspace marker",
              (workspace / ".dsh-workspace").is_file())
        check("status then reports it not installed",
              dsh.status(str(workspace), str(home))["skill_installed"] is False)
        empty = dsh.uninstall(str(workspace), str(home))
        check("uninstalling twice is harmless", empty["skill_removed"] is False)
        print()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return report_summary()


def report_summary():
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
