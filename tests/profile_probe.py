#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Profile resolution probe: one machine, several targets.

Checks the resolution order that keeps profiles honest --
environment variable > active profile > top-level config > default -- and that
each profile gets its own daemon socket, so two profiles can never share a
session by accident.

Uses a throwaway config file and working directory; touches nothing outside
``PYAETHER_BRIDGE_HOME``.

Usage::

    python3 tests/profile_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
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


def main():
    print("== profile resolution probe ==")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyaether-profile-"))
    home = tmp / "home"
    project = tmp / "project"
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    project.mkdir()

    (home / "config.json").write_text(json.dumps({
        "transport": "docker",
        "container": "base-container",
        "default_profile": "lab",
        "profiles": {
            "lab": {"transport": "ssh", "ssh_host": "aether@lab-server"},
            "box": {"transport": "local", "container": "ignored-when-local"},
            "simtest": {"transport": "docker", "container": "sim-container",
                        "sim_backend": "alps", "alps_threads": "8"},
        },
    }, indent=2), encoding="utf-8")

    base_env = dict(os.environ)
    base_env["PYAETHER_BRIDGE_HOME"] = str(home)
    for key in ("PYAETHER_PROFILE", "PYAETHER_TRANSPORT", "PYAETHER_CONTAINER",
                "PYAETHER_SSH_HOST", "PYAETHER_SIM_BACKEND", "PYAETHER_ALPS_THREADS"):
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
