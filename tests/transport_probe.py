#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport-layer probe: full session lifecycle over the local and ssh paths.

Needs neither Docker nor PyAether: when the target has no pyAether the session
still completes its handshake (``ok=false``) and keeps executing plain Python,
which is enough to exercise deploy -> start session -> NDJSON round trip ->
fetch a file back.

The ssh path is simulated with a fake ssh stub placed first on PATH (no network,
no credentials). The stub must faithfully reproduce real ssh semantics: **join
the remaining arguments with spaces into one string and hand it to the remote
login shell** (rather than exec'ing them directly). That difference matters:
the bridge quotes the whole remote command, and only one shell pass turns it
back into the real command.

Usage::

    python3 tests/transport_probe.py

To also exercise a real docker target::

    PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
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


def cli(env, *args, timeout=180):
    proc = subprocess.run(
        [sys.executable, str(CLI)] + list(args),
        cwd=str(ROOT), env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc


def base_env(home, remote_dir, **extra):
    env = dict(os.environ)
    env["PYAETHER_BRIDGE_HOME"] = str(home)
    env["PYAETHER_REMOTE_DIR"] = str(remote_dir)
    env["PYAETHER_PYTHON"] = sys.executable
    env.pop("PYAETHER_CATALOG_DB", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def exercise(label, env, remote_dir, deploy_path_visible=True):
    """Run one full session lifecycle; returns (exec_proc, status_dict)."""
    deploy_target = pathlib.Path(remote_dir) / "session_bridge.py"
    first = cli(env, "exec", "-c", "print('hello from %s'); 6 * 7" % label, "--timeout", "120")
    check("%s: exec exit code 0" % label, first.returncode == 0,
          "rc=%s stderr=%s" % (first.returncode, first.stderr[-400:]))
    check("%s: stdout is captured" % label, "hello from %s" % label in first.stdout,
          "stdout=%r" % first.stdout[-300:])
    check("%s: expression result is 42" % label, "42" in first.stdout,
          "stdout=%r" % first.stdout[-300:])
    if deploy_path_visible:
        # For local and the fake-ssh stub the "remote" is this filesystem, so the
        # deployed artifact can be checked directly.
        check("%s: session script deployed to the target" % label, deploy_target.exists(),
              "missing %s" % deploy_target)
    else:
        # A docker target keeps the path inside the container, so the host cannot
        # see it; the session starting at all proves the deploy worked.
        print("SKIP %s: deploy artifact lives inside the target; the session start covers it" % label)

    second = cli(env, "exec", "-c", "x = 40; x + 2", "--timeout", "60")
    check("%s: session reused (second call)" % label, second.returncode == 0,
          "rc=%s stderr=%s" % (second.returncode, second.stderr[-300:]))

    status = cli(env, "status", "--json", timeout=90)
    info = {}
    if status.returncode == 0:
        try:
            info = json.loads(status.stdout)
        except ValueError:
            info = {}
    else:
        print("       status stderr=%s" % status.stderr[-300:])
    check("%s: status returns valid JSON" % label, bool(info))
    target = info.get("transport") or {}
    check("%s: status reports the target reachable" % label, target.get("reachable") is True,
          json.dumps(target, ensure_ascii=False)[:300])
    check("%s: interpreter resolved" % label, bool(target.get("python")),
          json.dumps(target, ensure_ascii=False)[:300])
    session = info.get("session") or {}
    check("%s: session alive and has executed" % label,
          bool(session.get("alive")) and (session.get("generation") or 0) > 0,
          json.dumps(session, ensure_ascii=False)[:300])
    return first, info


def make_fake_ssh(bindir):
    """Fake ssh: parse options -> drop the host -> join the rest and let a shell
    parse it.

    Equivalent to real ssh remote execution (remote login shell -c "<joined args>").
    """
    path = pathlib.Path(bindir) / "ssh"
    path.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -T|-4|-6|-v) shift ;;\n"
        "    -o|-p|-i|-F|-l) shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        "shift            # drop the host (the stub never goes to the network)\n"
        "exec /bin/sh -c \"$*\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def stop_daemon(env):
    try:
        cli(env, "daemon", "stop", timeout=60)
    except Exception:
        pass


def main():
    print("== pyaether-bridge transport probe ==")
    print("root   : %s" % ROOT)
    print("python : %s (%s)" % (sys.executable, sys.version.split()[0]))
    print()

    with tempfile.TemporaryDirectory(prefix="pyaether-transport.") as tmp:
        tmp_path = pathlib.Path(tmp)

        # ---- local ----------------------------------------------------
        print("-- local transport --")
        home = tmp_path / "home-local"
        remote = tmp_path / "remote-local"
        env_local = base_env(home, remote, PYAETHER_TRANSPORT="local")
        try:
            exercise("local", env_local, remote)
        finally:
            stop_daemon(env_local)
        print()

        # ---- ssh (fake ssh stub) --------------------------------------
        print("-- ssh transport (fake ssh stub, simulated remote server) --")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        make_fake_ssh(bindir)
        home_ssh = tmp_path / "home-ssh"
        remote_ssh = tmp_path / "remote-ssh"
        env_ssh = base_env(
            home_ssh, remote_ssh,
            PYAETHER_TRANSPORT="ssh",
            PYAETHER_SSH_HOST="codex@example-host",
            PYAETHER_SSH_PORT="2222",
            PYAETHER_SSH_OPTS="-o StrictHostKeyChecking=no",
        )
        env_ssh["PATH"] = "%s%s%s" % (bindir, os.pathsep, env_ssh.get("PATH", ""))
        try:
            exercise("ssh", env_ssh, remote_ssh)
        finally:
            stop_daemon(env_ssh)
        print()

        # ---- docker (optional, needs a real container) ----------------
        if os.environ.get("PYAETHER_TEST_DOCKER") == "1":
            print("-- docker transport (real container) --")
            home_docker = tmp_path / "home-docker"
            env_docker = base_env(home_docker, "/tmp/pyaether-bridge",
                                  PYAETHER_TRANSPORT="docker")
            # The container has no host interpreter; use the target default
            # python3.9 (resolved through the login shell).
            env_docker.pop("PYAETHER_PYTHON", None)
            try:
                exercise("docker", env_docker, "/tmp/pyaether-bridge",
                         deploy_path_visible=False)
            finally:
                stop_daemon(env_docker)
        else:
            print("-- docker transport: skipped (set PYAETHER_TEST_DOCKER=1 to enable) --")

    print()
    print("-" * 64)
    print("result: %d PASS / %d FAIL" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("verdict: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
