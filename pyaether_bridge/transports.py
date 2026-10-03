# -*- coding: utf-8 -*-
"""Execution-target transport layer: abstracts "where PyAether lives" into the
docker / ssh / local variants.

The daemon relies on only five actions from this module:

    describe()                dict used for status display
    probe()                   target reachability and interpreter information
    write_file(path, data)    put session_bridge.py onto the target
    open_session(inner)       start a persistent stdio session (NDJSON)
    fetch_file(path, local)   fetch a file back from the target (used by `api sync-live`)

The three deployments map to real-world setups:

    docker  Aether installed in a container (default `empyrean-gui`), entered via `docker exec`
    ssh     installed on a company/lab server, entered via `ssh` (using your own SSH config and keys)
    local   the bridge runs on the Linux machine that has Aether, spawning a plain local process

All three start through a login shell (`bash -lc`) because Aether's ``AETHER_*``,
``LD_LIBRARY_PATH`` and ``PYTHONPATH`` are provided by the image/server profile.
This module never reads or stores credentials; SSH authentication is left to ssh
itself (key + ssh-agent recommended), with BatchMode forced so it never blocks in
the background waiting for a password.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import subprocess

from . import config

RUN_TIMEOUT = 60.0


class TransportError(RuntimeError):
    """Transport failure (missing command, unreachable target, failed deploy, ...)."""


def _run(command, data=None, timeout=RUN_TIMEOUT):
    try:
        return subprocess.run(
            command, input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise TransportError("command not found %r: %s" % (command[0], exc))
    except subprocess.TimeoutExpired:
        raise TransportError("%s timed out (%.0fs)" % (command[0], timeout))


def _text(raw):
    return (raw or b"").decode("utf-8", "replace").strip()


class Transport:
    kind = "base"

    def __init__(self, remote_dir, python):
        self.remote_dir = remote_dir
        self.python = python
        self.script_path = os.path.join(remote_dir, "session_bridge.py")

    # ---- display --------------------------------------------------------
    def describe(self):
        return {
            "kind": self.kind,
            "remote_dir": self.remote_dir,
            "python": self.python,
            "script": self.script_path,
        }

    def label(self):
        return self.kind

    # ---- actions --------------------------------------------------------
    def probe(self):
        raise NotImplementedError

    def write_file(self, path, data):
        raise NotImplementedError

    def open_session(self, inner_command):
        raise NotImplementedError

    def fetch_file(self, path, local_path):
        raise NotImplementedError

    # ---- shared ---------------------------------------------------------
    def session_command(self):
        """The command that is actually executed: login shell + Aether's bundled interpreter."""
        return "exec %s %s" % (self.python, shlex.quote(self.script_path))


class DockerTransport(Transport):
    kind = "docker"

    def __init__(self, container, remote_dir, python, license_server=None):
        super().__init__(remote_dir, python)
        self.container = container
        self.license_server = license_server

    def label(self):
        return "docker:%s" % self.container

    def describe(self):
        info = super().describe()
        info["container"] = self.container
        return info

    def _exec(self, command, data=None, timeout=RUN_TIMEOUT):
        return _run(["docker", "exec", "-i", self.container, "sh", "-c", command],
                    data=data, timeout=timeout)

    def _login(self, command, timeout=RUN_TIMEOUT):
        """Run inside a login shell: Aether's interpreter and LD_LIBRARY_PATH both come from the profile."""
        return _run(["docker", "exec", "-i", self.container, "bash", "-lc", command],
                    timeout=timeout)

    def probe(self):
        proc = _run(["docker", "ps", "-a", "--format",
                     "{{.Names}}\t{{.Status}}\t{{.Image}}"])
        if proc.returncode != 0:
            raise TransportError("docker ps failed: %s" % _text(proc.stderr))
        targets = {}
        for line in _text(proc.stdout).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            targets[parts[0]] = {
                "running": parts[1].startswith("Up"),
                "status": parts[1],
                "image": parts[2],
            }
        target = targets.get(self.container)
        info = {
            "reachable": bool(target and target["running"]),
            "targets": targets,
            "detail": ("container %s is not running" % self.container) if not (target and target["running"])
                      else "",
        }
        info["python"] = self._probe_python()
        return info

    def _probe_python(self):
        proc = self._login("command -v %s" % shlex.quote(self.python), timeout=30.0)
        return _text(proc.stdout) if proc.returncode == 0 else ""

    def write_file(self, path, data):
        command = "mkdir -p %s && cat > %s" % (
            shlex.quote(os.path.dirname(path)), shlex.quote(path))
        proc = self._exec(command, data=data)
        if proc.returncode != 0:
            raise TransportError("deployment into container %s failed: %s" % (self.container, _text(proc.stderr)))

    def open_session(self, inner_command):
        command = ["docker", "exec", "-i", "-w", "/tmp"]
        if self.license_server:
            # Only override when the user configured it explicitly; otherwise keep the container's own LM_LICENSE_FILE.
            command += ["-e", "LM_LICENSE_FILE=%s" % self.license_server]
        command += [self.container, "bash", "-lc", inner_command]
        return self._popen(command)

    def fetch_file(self, path, local_path):
        proc = _run(["docker", "cp", "%s:%s" % (self.container, path), str(local_path)],
                    timeout=120.0)
        if proc.returncode != 0:
            raise TransportError("docker cp failed: %s" % _text(proc.stderr))

    @staticmethod
    def _popen(command):
        try:
            return subprocess.Popen(
                command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TransportError("docker command not found: %s" % exc)


class SSHTransport(Transport):
    kind = "ssh"

    def __init__(self, host, remote_dir, python, port=None, options=None,
                 license_server=None):
        super().__init__(remote_dir, python)
        self.host = host
        self.port = str(port) if port else None
        self.options = shlex.split(options) if options else []
        self.license_server = license_server

    def label(self):
        return "ssh:%s" % self.host

    def describe(self):
        info = super().describe()
        info["host"] = self.host
        info["port"] = self.port
        return info

    def _base(self):
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.port:
            command += ["-p", self.port]
        command += self.options
        command += [self.host]
        return command

    def _remote(self, command, data=None, timeout=RUN_TIMEOUT):
        # ssh joins the arguments with spaces and hands them to the remote shell, so the whole command must be quoted as one unit.
        return _run(self._base() + ["bash", "-lc", shlex.quote(command)],
                    data=data, timeout=timeout)

    def probe(self):
        info = {"reachable": False, "targets": {}, "detail": "", "python": ""}
        proc = self._remote("true", timeout=30.0)
        if proc.returncode != 0:
            info["detail"] = ("ssh %s unreachable: %s" % (self.host, _text(proc.stderr))
                              or "ssh connection failed")
            return info
        info["reachable"] = True
        proc = self._remote("command -v %s" % shlex.quote(self.python), timeout=30.0)
        if proc.returncode == 0:
            info["python"] = _text(proc.stdout)
        else:
            info["detail"] = "interpreter not found on %s: %s" % (self.host, self.python)
        return info

    def write_file(self, path, data):
        command = "mkdir -p %s && cat > %s" % (
            shlex.quote(os.path.dirname(path)), shlex.quote(path))
        proc = self._remote(command, data=data)
        if proc.returncode != 0:
            raise TransportError("deployment to %s failed: %s" % (self.host, _text(proc.stderr)))

    def open_session(self, inner_command):
        inner = inner_command
        if self.license_server:
            inner = "export LM_LICENSE_FILE=%s; %s" % (
                shlex.quote(self.license_server), inner)
        try:
            return subprocess.Popen(
                self._base() + ["bash", "-lc", shlex.quote(inner)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TransportError("ssh command not found: %s" % exc)

    def fetch_file(self, path, local_path):
        proc = _run(self._base() + ["bash", "-lc", shlex.quote("cat %s" % shlex.quote(path))],
                    timeout=120.0)
        if proc.returncode != 0:
            raise TransportError("failed to fetch a file from %s: %s" % (self.host, _text(proc.stderr)))
        with open(local_path, "wb") as handle:
            handle.write(proc.stdout)


class LocalTransport(Transport):
    kind = "local"

    def label(self):
        return "local"

    def probe(self):
        info = {"reachable": True, "targets": {}, "detail": "", "python": ""}
        found = shutil.which(self.python) or (
            self.python if os.path.isabs(self.python) and os.path.exists(self.python) else "")
        info["python"] = found
        if not found:
            info["detail"] = "interpreter %s not found on this machine" % self.python
        return info

    def write_file(self, path, data):
        pathlib.Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def open_session(self, inner_command):
        try:
            return subprocess.Popen(
                ["bash", "-lc", inner_command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TransportError("bash not found: %s" % exc)

    def fetch_file(self, path, local_path):
        shutil.copyfile(path, local_path)


def build(kind=None):
    """Build the transport from config; the argument lets tests override it explicitly, otherwise config is read."""
    kind = (kind or config.TRANSPORT or "docker").lower()
    if kind == "docker":
        return DockerTransport(config.CONTAINER, config.REMOTE_DIR, config.PYTHON,
                               license_server=config.LICENSE_SERVER)
    if kind == "ssh":
        if not config.SSH_HOST:
            raise TransportError(
                "transport=ssh requires PYAETHER_SSH_HOST to be set (e.g. user@server)")
        return SSHTransport(config.SSH_HOST, config.REMOTE_DIR, config.PYTHON,
                            port=config.SSH_PORT, options=config.SSH_OPTS,
                            license_server=config.LICENSE_SERVER)
    if kind == "local":
        return LocalTransport(config.REMOTE_DIR, config.PYTHON)
    raise TransportError("unknown transport %r (expected docker / ssh / local)" % kind)
