#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Profile resolution probe: one machine, several targets.

Checks the resolution order that keeps profiles honest --
environment variable > active profile > top-level config > default -- and the
things that stop two targets from being confused with each other:

  * each profile gets its own daemon socket, lock and scratch paths;
  * version-dependent artifacts (symbol catalog, docs) are pinned per profile;
  * ``PYAETHER_REQUIRE_EXPLICIT_PROFILE`` refuses an inherited profile;
  * ``expected_*`` values are asserted against the target, and an unverifiable
    claim fails rather than passing quietly;
  * the target fingerprint separates two profiles that point elsewhere.

Uses a throwaway config file and working directory; touches nothing outside
``PYAETHER_BRIDGE_HOME`` and a temp directory.

Usage::

    python3 tests/profile_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "pyaether"

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       ("\n       %s" % detail) if (detail and not condition) else ""))


def run_cli(env, cwd, *args):
    return subprocess.run([sys.executable, str(CLI)] + list(args),
                          cwd=str(cwd), env=env, timeout=60,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def load_settings(env, cwd):
    """Ask the CLI for the resolved settings (no daemon involved)."""
    proc = run_cli(env, cwd, "profile", "show", "--json")
    if proc.returncode != 0:
        return None, proc
    return json.loads(proc.stdout), proc


def _python(env, cwd, code):
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          timeout=60, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def config_value(env, cwd, expression):
    """Evaluate one expression against the config module in a clean process."""
    proc = _python(env, cwd,
                   "import sys; sys.path.insert(0, %r);"
                   "from pyaether_bridge import config;"
                   "print(%s)" % (str(ROOT), expression))
    return proc.stdout.strip(), proc


def resolve_docs(env, cwd, roots):
    """Call resolve_docs_dir() against a fake set of installed Aether trees."""
    return _python(env, cwd,
                   "import sys, pathlib; sys.path.insert(0, %r);"
                   "from pyaether_bridge import config as c;"
                   "c._INSTALL_ROOTS = (pathlib.Path(%r),);\n"
                   "try:\n"
                   "    print('RESOLVED', c.resolve_docs_dir())\n"
                   "except c.ConfigError as exc:\n"
                   "    print('REFUSED', exc)\n" % (str(ROOT), str(roots)))


def main():
    print("== profile resolution probe ==")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyaether-profile-"))
    home = tmp / "home"
    project = tmp / "project"
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    project.mkdir()

    # A fake installed Aether tree, so a profile-level docs_dir can be checked
    # without a real installation.
    fake_docs = tmp / "fake-aether" / "tools" / "pyaether" / "docs" / "html"
    fake_docs.mkdir(parents=True)
    (fake_docs / "objects.inv").write_text("probe\n", encoding="utf-8")

    (home / "config.json").write_text(json.dumps({
        "transport": "docker",
        "container": "base-container",
        "default_profile": "lab",
        "profiles": {
            "lab": {"transport": "ssh", "ssh_host": "aether@lab-server"},
            "box": {"transport": "local", "container": "ignored-when-local"},
            "simtest": {"transport": "docker", "container": "sim-container",
                        "sim_backend": "alps", "alps_threads": "8"},
            "v26": {"transport": "local", "aether_version": "2026.03"},
            "v25": {"transport": "local", "aether_version": "2025.10"},
            "pinned": {"transport": "local", "aether_version": "2099.01",
                       "docs_dir": str(fake_docs)},
            "here": {"transport": "local", "expected_hostname": socket.gethostname()},
            "elsewhere": {"transport": "local", "expected_hostname": "not-this-host"},
            "image": {"transport": "docker", "container": "empyrean-gui",
                      "expected_image": "some-image:tag"},
        },
    }, indent=2), encoding="utf-8")

    base_env = dict(os.environ)
    base_env["PYAETHER_BRIDGE_HOME"] = str(home)
    for key in ("PYAETHER_PROFILE", "PYAETHER_TRANSPORT", "PYAETHER_CONTAINER",
                "PYAETHER_SSH_HOST", "PYAETHER_SIM_BACKEND", "PYAETHER_ALPS_THREADS",
                "PYAETHER_REMOTE_DIR", "PYAETHER_AETHER_VERSION", "PYAETHER_DOCS_DIR",
                "PYAETHER_CATALOG_DB", "PYAETHER_REQUIRE_EXPLICIT_PROFILE",
                "PYAETHER_EXPECTED_HOSTNAME", "PYAETHER_ALLOW_IDENTITY_MISMATCH",
                "PYAETHER_ALLOW_STALE_DAEMON", "PYAETHER_BRIDGE_NO_AUTOSTART"):
        base_env.pop(key, None)

    print("home    : %s" % home)
    print("project : %s" % project)
    print()

    # ---- 1. default_profile ----------------------------------------------
    print("-- 1. default_profile from the config file --")
    settings, proc = load_settings(base_env, project)
    check("profile show works", settings is not None,
          proc.stderr[-300:] if proc else "")
    if settings:
        check("default_profile is active", settings.get("active") == "lab",
              json.dumps(settings, ensure_ascii=False)[:300])
        check("profile transport wins over the top-level transport",
              settings["resolved"]["transport"] == "ssh",
              settings["resolved"]["transport"])
        check("profile value is used", settings["resolved"]["ssh_host"] == "aether@lab-server",
              settings["resolved"]["ssh_host"])
    print()

    # ---- 2. binding file --------------------------------------------------
    print("-- 2. .pyaether-profile binding --")
    bind = run_cli(base_env, project, "profile", "bind", "box")
    check("profile bind succeeds", bind.returncode == 0, bind.stderr[-200:])
    check("binding file is written", (project / ".pyaether-profile").is_file(),
          str(project / ".pyaether-profile"))
    settings, proc = load_settings(base_env, project)
    check("binding overrides default_profile", (settings or {}).get("active") == "box",
          json.dumps(settings or {}, ensure_ascii=False)[:200])
    check("bound profile's settings apply",
          (settings or {}).get("resolved", {}).get("transport") == "local",
          json.dumps((settings or {}).get("resolved", {}), ensure_ascii=False)[:200])

    nested = project / "deep" / "nested"
    nested.mkdir(parents=True)
    settings, _ = load_settings(base_env, nested)
    check("binding is found from a nested directory", (settings or {}).get("active") == "box",
          json.dumps(settings or {}, ensure_ascii=False)[:200])

    unknown = run_cli(base_env, project, "profile", "bind", "does-not-exist")
    check("binding an unknown profile is refused", unknown.returncode == 1,
          "rc=%s" % unknown.returncode)

    cleared = run_cli(base_env, project, "profile", "clear")
    check("profile clear removes the binding", cleared.returncode == 0
          and not (project / ".pyaether-profile").exists(), cleared.stderr[-200:])
    settings, _ = load_settings(base_env, project)
    check("after clear the default_profile applies again",
          (settings or {}).get("active") == "lab",
          json.dumps(settings or {}, ensure_ascii=False)[:200])
    print()

    # ---- 3. environment override -----------------------------------------
    print("-- 3. environment variable wins --")
    env = dict(base_env)
    env["PYAETHER_PROFILE"] = "simtest"
    settings, _ = load_settings(env, project)
    check("PYAETHER_PROFILE selects a profile", (settings or {}).get("active") == "simtest",
          json.dumps(settings or {}, ensure_ascii=False)[:200])
    check("profile carried the simulator settings",
          (settings or {}).get("resolved", {}).get("sim_backend") == "alps",
          json.dumps((settings or {}).get("resolved", {}), ensure_ascii=False)[:250])
    env["PYAETHER_CONTAINER"] = "from-env"
    settings, _ = load_settings(env, project)
    check("environment variable beats the profile",
          (settings or {}).get("resolved", {}).get("container") == "from-env",
          json.dumps((settings or {}).get("resolved", {}), ensure_ascii=False)[:250])
    print()

    # ---- 4. per-profile daemon isolation ---------------------------------
    print("-- 4. per-profile runtime isolation --")
    env_lab = dict(base_env)
    env_lab["PYAETHER_PROFILE"] = "lab"
    env_box = dict(base_env)
    env_box["PYAETHER_PROFILE"] = "box"
    out_lab = subprocess.run([sys.executable, "-c",
                              "import sys; sys.path.insert(0, %r);"
                              "from pyaether_bridge import config;"
                              "print(config.DAEMON_SOCK)" % str(ROOT)],
                             cwd=str(project), env=env_lab, timeout=60,
                             stdout=subprocess.PIPE, text=True).stdout.strip()
    out_box = subprocess.run([sys.executable, "-c",
                              "import sys; sys.path.insert(0, %r);"
                              "from pyaether_bridge import config;"
                              "print(config.DAEMON_SOCK)" % str(ROOT)],
                             cwd=str(project), env=env_box, timeout=60,
                             stdout=subprocess.PIPE, text=True).stdout.strip()
    check("two profiles get different daemon sockets", out_lab != out_box,
          "%s vs %s" % (out_lab, out_box))
    check("the socket lives under the profile directory",
          "/profiles/lab/" in out_lab and "/profiles/box/" in out_box,
          "%s | %s" % (out_lab, out_box))

    no_profile = dict(base_env)
    no_profile["PYAETHER_BRIDGE_HOME"] = str(tmp / "flat-home")
    (tmp / "flat-home").mkdir()
    out_flat = subprocess.run([sys.executable, "-c",
                               "import sys; sys.path.insert(0, %r);"
                               "from pyaether_bridge import config;"
                               "print(config.DAEMON_SOCK)" % str(ROOT)],
                              cwd=str(tmp), env=no_profile, timeout=60,
                              stdout=subprocess.PIPE, text=True).stdout.strip()
    check("with no profile the socket stays where it always was",
          out_flat.endswith("daemon.sock") and "/profiles/" not in out_flat, out_flat)
    print()

    # ---- 5. per-profile lock and scratch paths ---------------------------
    print("-- 5. per-profile lock and scratch paths --")
    lock_lab, _ = config_value(env_lab, project, "config.DAEMON_LOCK")
    lock_box, _ = config_value(env_box, project, "config.DAEMON_LOCK")
    check("the startup lock is per profile, not shared",
          lock_lab != lock_box and "/profiles/lab/" in lock_lab
          and "/profiles/box/" in lock_box, "%s | %s" % (lock_lab, lock_box))

    lab_settings, _ = load_settings(env_lab, project)
    box_settings, _ = load_settings(env_box, project)
    lab_remote = (lab_settings or {}).get("resolved", {}).get("remote_dir", "")
    box_remote = (box_settings or {}).get("resolved", {}).get("remote_dir", "")
    check("two profiles do not share the deployed session script directory",
          lab_remote != box_remote, "%s | %s" % (lab_remote, box_remote))
    check("the profile name is part of those paths",
          lab_remote.endswith("-lab") and box_remote.endswith("-box"),
          "%s | %s" % (lab_remote, box_remote))
    check("staged simulator runs are separated the same way",
          (lab_settings or {}).get("resolved", {}).get("sim_workdir", "").endswith("-lab"),
          (lab_settings or {}).get("resolved", {}).get("sim_workdir", ""))
    explicit = dict(env_lab)
    explicit["PYAETHER_REMOTE_DIR"] = "/tmp/exactly-here"
    explicit_settings, _ = load_settings(explicit, project)
    check("an explicit remote_dir is used verbatim",
          (explicit_settings or {}).get("resolved", {}).get("remote_dir")
          == "/tmp/exactly-here",
          (explicit_settings or {}).get("resolved", {}).get("remote_dir", ""))
    flat_remote, _ = config_value(no_profile, tmp, "config.REMOTE_DIR")
    check("with no profile the paths keep their historical values",
          flat_remote == "/tmp/pyaether-bridge", flat_remote)
    print()

    # ---- 6. version-dependent artifacts ----------------------------------
    print("-- 6. version-pinned catalog and docs --")
    v26 = dict(base_env)
    v26["PYAETHER_PROFILE"] = "v26"
    v25 = dict(base_env)
    v25["PYAETHER_PROFILE"] = "v25"
    cat26, _ = config_value(v26, project, "config.CATALOG_DB")
    cat25, _ = config_value(v25, project, "config.CATALOG_DB")
    check("each declared version gets its own catalog", cat26 != cat25,
          "%s | %s" % (cat26, cat25))
    check("the profile and the version are both in the catalog path",
          "catalog-2026.03.sqlite" in cat26 and "/profiles/v26/" in cat26, cat26)
    cat_lab, _ = config_value(env_lab, project, "config.CATALOG_DB")
    check("without a declared version the historical catalog is kept",
          cat_lab.endswith("data/catalog.sqlite"), cat_lab)

    pinned = dict(base_env)
    pinned["PYAETHER_PROFILE"] = "pinned"
    pinned_settings, pinned_proc = load_settings(pinned, project)
    check("a profile-level docs_dir is honoured, not ignored",
          (pinned_settings or {}).get("docs_dir") == str(fake_docs),
          "%s (stderr %s)" % ((pinned_settings or {}).get("docs_dir"),
                              pinned_proc.stderr[-200:]))
    check("the pinned profile's catalog is version-keyed",
          (pinned_settings or {}).get("catalog_db", "").endswith("catalog-2099.01.sqlite"),
          (pinned_settings or {}).get("catalog_db", ""))

    installs = tmp / "installs"
    for name in ("Aether_2026.03_x", "Aether_2025.10_y"):
        version_docs = installs / name / "tools" / "pyaether" / "docs" / "html"
        version_docs.mkdir(parents=True)
        (version_docs / "objects.inv").write_text("probe\n", encoding="utf-8")
    ambiguous = resolve_docs(base_env, project, installs)
    check("two installed Aether trees with nothing pinned is refused, not guessed",
          ambiguous.stdout.startswith("REFUSED") and "ambiguous" in ambiguous.stdout,
          (ambiguous.stdout + ambiguous.stderr)[:300])
    single = tmp / "single-install"
    single_docs = single / "Aether_2026.03_z" / "tools" / "pyaether" / "docs" / "html"
    single_docs.mkdir(parents=True)
    (single_docs / "objects.inv").write_text("probe\n", encoding="utf-8")
    only_one = resolve_docs(base_env, project, single)
    check("exactly one installed tree is resolved without a pin",
          only_one.stdout.startswith("RESOLVED") and "Aether_2026.03_z" in only_one.stdout,
          (only_one.stdout + only_one.stderr)[:300])
    print()

    # ---- 7. explicit-profile guard ---------------------------------------
    print("-- 7. explicit-profile guard --")
    guard = dict(base_env)
    guard["PYAETHER_REQUIRE_EXPLICIT_PROFILE"] = "1"
    guard["PYAETHER_BRIDGE_NO_AUTOSTART"] = "1"
    netlist = tmp / "probe.cir"
    netlist.write_text("probe netlist\n", encoding="utf-8")
    refused = run_cli(guard, project, "sim", "run", str(netlist))
    check("a write command refuses a profile that was only inherited",
          refused.returncode == 1 and "REQUIRE_EXPLICIT_PROFILE" in refused.stderr,
          "rc=%s %s" % (refused.returncode, refused.stderr[-200:]))
    read_only = run_cli(guard, project, "status")
    check("read-only commands are unaffected by the guard", read_only.returncode == 0,
          read_only.stderr[-200:])
    named = dict(guard)
    named["PYAETHER_PROFILE"] = "box"
    named_run = run_cli(named, project, "exec", "-c", "1+1")
    check("naming the profile satisfies the guard",
          "REQUIRE_EXPLICIT_PROFILE" not in named_run.stderr, named_run.stderr[-200:])
    allowed_show = run_cli(named, project, "profile", "show")
    check("a named profile is reported as confirmed", "[confirmed]" in allowed_show.stdout,
          allowed_show.stdout[-200:])
    inherited_show = run_cli(guard, project, "profile", "show")
    check("an inherited profile warns when the guard is on",
          "REQUIRE_EXPLICIT_PROFILE is set" in inherited_show.stdout,
          inherited_show.stdout[-250:])
    print()

    # ---- 8. target identity ----------------------------------------------
    print("-- 8. target identity check --")
    here = dict(base_env)
    here["PYAETHER_PROFILE"] = "here"
    ok_verify = run_cli(here, project, "profile", "verify")
    check("a matching expected_hostname verifies", ok_verify.returncode == 0,
          "rc=%s %s" % (ok_verify.returncode, (ok_verify.stdout + ok_verify.stderr)[-250:]))
    check("the check names the observed host",
          socket.gethostname() in ok_verify.stdout, ok_verify.stdout[-250:])

    away = dict(base_env)
    away["PYAETHER_PROFILE"] = "elsewhere"
    bad_verify = run_cli(away, project, "profile", "verify")
    check("a mismatching expected_hostname fails with exit 1", bad_verify.returncode == 1,
          "rc=%s" % bad_verify.returncode)
    check("the failure names the field and both values",
          "mismatch" in bad_verify.stdout and "not-this-host" in bad_verify.stdout,
          bad_verify.stdout[-300:])

    override = dict(away)
    override["PYAETHER_ALLOW_IDENTITY_MISMATCH"] = "1"
    still_bad = run_cli(override, project, "profile", "verify")
    check("verify still reports the truth while the override is set",
          still_bad.returncode == 1 and "mismatch" in still_bad.stdout,
          still_bad.stdout[-250:])

    unconfigured = dict(base_env)
    unconfigured["PYAETHER_PROFILE"] = "box"
    none_set = run_cli(unconfigured, project, "profile", "verify")
    check("nothing configured means nothing is asserted", none_set.returncode == 0,
          "rc=%s %s" % (none_set.returncode, none_set.stdout[-200:]))

    image = dict(base_env)
    image["PYAETHER_PROFILE"] = "image"
    image_check = run_cli(image, project, "profile", "verify")
    check("a declared expectation the target cannot report fails the check",
          image_check.returncode == 1
          and ("unverifiable" in image_check.stdout or "mismatch" in image_check.stdout),
          image_check.stdout[-300:])
    print()

    # ---- 9. target fingerprint -------------------------------------------
    print("-- 9. target fingerprint --")
    fp_lab, _ = config_value(env_lab, project, "config.TARGET_FINGERPRINT")
    fp_box, _ = config_value(env_box, project, "config.TARGET_FINGERPRINT")
    check("two profiles pointing at different targets differ",
          fp_lab != fp_box and len(fp_lab) == 16, "%s | %s" % (fp_lab, fp_box))
    fp_again, _ = config_value(env_lab, project, "config.TARGET_FINGERPRINT")
    check("the fingerprint is stable for the same profile", fp_lab == fp_again)

    state = run_cli(env_box, project, "status", "--json")
    payload = json.loads(state.stdout) if state.returncode == 0 else {}
    target = payload.get("target") or {}
    check("status reports the resolved fingerprint and no stale daemon",
          target.get("resolved_fingerprint") == fp_box and target.get("matches") is True,
          json.dumps(target, ensure_ascii=False))
    check("status reports how the profile was selected",
          (payload.get("profile") or {}).get("source") == "PYAETHER_PROFILE",
          json.dumps(payload.get("profile"), ensure_ascii=False))
    print()

    print("-" * 64)
    print("result: %d PASS / %d FAIL" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
