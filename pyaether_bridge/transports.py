# -*- coding: utf-8 -*-
"""Execution-target transport layer: abstracts "where PyAether lives" into the
docker / ssh / local variants.

The daemon relies on only six actions from this module:

    describe()                dict used for status display
    probe()                   target reachability and interpreter information
    write_file(path, data)    put session_bridge.py onto the target
    open_session(inner)       start a persistent stdio session (NDJSON)
    fetch_file(path, local)   fetch a file back from the target (used by `api sync-live`)
    run_command(command)      run one shell command on the target and capture its
                              output (used by `sim run` for external simulators)

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
import signal
import subprocess
import time
import time

from . import config

RUN_TIMEOUT = 60.0
COMMAND_TIMEOUT = 600.0


class TransportError(RuntimeError):
    """Transport failure (missing command, unreachable target, failed deploy, ...)."""


class TransportTimeout(TransportError):
    """The command did not finish in time.

    Kept separate from other transport errors because a timeout is the one case
    where the target may still be *running* the command: killing the local
    client (docker exec / ssh) does not kill the process on the other side, so
    the caller has to ask for a cleanup by marker.
    """

    timed_out = True


def _run(command, data=None, timeout=RUN_TIMEOUT):
    try:
        return subprocess.run(
            command, input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise TransportError("command not found %r: %s" % (command[0], exc))
    except subprocess.TimeoutExpired:
        raise TransportTimeout("%s timed out (%.0fs)" % (command[0], timeout))


def _text(raw):
    return (raw or b"").decode("utf-8", "replace").strip()


def _decode(raw):
    return (raw or b"").decode("utf-8", "replace")


def _command_result(command, returncode, stdout, stderr, elapsed, timed_out=False):
    return {
        "command": command,
        "returncode": returncode,
        "stdout": _decode(stdout),
        "stderr": _decode(stderr),
        "elapsed_s": round(elapsed, 3),
        "timed_out": bool(timed_out),
    }


def _run_capture(command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
    """Run a command and keep its output, even when it times out.

    Unlike :func:`_run` this never raises on a non-zero exit or a timeout: a
    simulator that fails is a result to report, not a transport error.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise TransportError("command not found %r: %s" % (command[0], exc))
    except subprocess.TimeoutExpired as exc:
        return _command_result(
            " ".join(command), None, exc.stdout, exc.stderr,
            time.time() - started, timed_out=True,
        )
    return _command_result(
        " ".join(command), proc.returncode, proc.stdout, proc.stderr,
        time.time() - started,
    )


def run_on_host(command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
    """Run one command on the machine that hosts the daemon (never on the target).

    Used by `sim run --side host` for open-source tools, independently of the
    docker/ssh/local transport that talks to PyAether.
    """
    merged = dict(os.environ)
    merged.update({key: str(value) for key, value in (env or {}).items()})
    return _run_capture(["bash", "-lc", command], cwd=cwd, env=merged, timeout=timeout)


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

    def run_command(self, command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
        raise NotImplementedError

    # ---- shared ---------------------------------------------------------
    def session_command(self):
        """The command that is actually executed: login shell + Aether's bundled interpreter."""
        return "exec %s %s" % (self.python, shlex.quote(self.script_path))

    def run(self, command, timeout=RUN_TIMEOUT, env=None):
        """Run one shell command on the target -> {"rc", "stdout", "stderr"}.

        The simulator layer stages a netlist with ``write_file``, invokes the
        simulator here, and pulls artifacts back with ``fetch_file``; so
        simulators reuse whatever target the bridge already talks to
        (container / SSH host / this machine).
        """
        raise NotImplementedError

    @staticmethod
    def _wrap_env(command, env):
        """Prefix ``VAR=value`` assignments so the command sees them."""
        if not env:
            return command
        prefix = " ".join(
            "%s=%s" % (key, shlex.quote(str(value))) for key, value in sorted(env.items())
        )
        return "%s %s" % (prefix, command)

    @staticmethod
    def _split(proc):
        return {"rc": proc.returncode, "stdout": _text(proc.stdout), "stderr": _text(proc.stderr)}

    @staticmethod
    def _pidfile(run_dir):
        return os.path.join(run_dir, "sim.pid")

    @classmethod
    def _tracked_command(cls, command, run_dir):
        """Run ``command`` in its own process group and record its PID.

        ``setsid`` detaches the command into a new session, so the whole tree
        (a simulator plus whatever it forks) shares one process group. Killing
        the local client is not enough on its own: the process on the other side
        keeps running, and killing only the wrapper leaves the real worker alive
        (measured -- `pkill -f` on the wrapper left `sleep` running). With a
        process group we can kill the tree in one shot.

        ``setsid`` is Linux-only (macOS has no such binary), so the snippet
        falls back to a plain background job. That is still correct for the
        local transport, which already starts the whole thing in a new session;
        both paths were measured to propagate the exit status unchanged.
        """
        pidfile = shlex.quote(cls._pidfile(run_dir))
        inner = shlex.quote(command)
        return (
            "if command -v setsid >/dev/null 2>&1; then setsid sh -c {inner} &"
            " else sh -c {inner} & fi; _p=$!; echo $_p > {pidfile}; wait $_p"
        ).format(inner=inner, pidfile=pidfile)

    @staticmethod
    def _kill_group_script(run_dir):
        """Shell snippet: kill the recorded process group, then sweep by path."""
        pidfile = shlex.quote(Transport._pidfile(run_dir))
        return (
            "p=$(cat {pidfile} 2>/dev/null);"
            " if [ -n \"$p\" ]; then"
            " kill -TERM -\"$p\" 2>/dev/null || true;"
            " sleep 1;"
            " kill -KILL -\"$p\" 2>/dev/null || true;"
            " fi;"
            " pkill -f {run_dir} >/dev/null 2>&1 || true;"
            " rm -f {pidfile};"
        ).format(pidfile=pidfile, run_dir=shlex.quote(run_dir))

    @staticmethod
    def _leftover_check_script(run_dir):
        return "ps -eo pid,args 2>/dev/null | grep -F %s | grep -v grep || true" % (
            shlex.quote(run_dir))

    def cleanup(self, run_dir):
        """Best-effort kill of everything the run started on the target.

        Returns a short note describing what was actually observed afterwards;
        never raises, because this runs on the failure path.
        """
        raise NotImplementedError


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

    def run_command(self, command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
        argv = ["docker", "exec", "-i"]
        if cwd:
            argv += ["-w", cwd]
        for key, value in sorted((env or {}).items()):
            argv += ["-e", "%s=%s" % (key, value)]
        argv += [self.container, "bash", "-lc", command]
        return _run_capture(argv, timeout=timeout)

    def run(self, command, timeout=RUN_TIMEOUT, env=None, run_dir=None):
        command = self._wrap_env(command, env)
        if run_dir:
            command = self._tracked_command(command, run_dir)
        return self._split(self._login(command, timeout=timeout))

    def cleanup(self, run_dir):
        # Killing the host-side `docker exec` leaves the container process
        # running (measured: `sleep 45` survived a 3 s timeout), so the kill has
        # to happen inside the container, against the recorded process group.
        try:
            self._exec(self._kill_group_script(run_dir), timeout=60.0)
            leftovers = self._exec(self._leftover_check_script(run_dir), timeout=30.0)
        except TransportError as exc:
            return "cleanup failed: %s" % exc
        remaining = _text(leftovers.stdout).strip()
        if remaining:
            return ("killed process group, but processes matching %s remain in %s: %s"
                    % (run_dir, self.container, remaining.replace("\n", "; ")[:200]))
        return "killed process group for %s in container %s (no leftovers)" % (
            run_dir, self.container)

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

    def run_command(self, command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
        inner = command
        if env:
            inner = "export %s; %s" % (
                " ".join("%s=%s" % (key, shlex.quote(str(value)))
                         for key, value in sorted(env.items())), inner)
        if cwd:
            inner = "cd %s && %s" % (shlex.quote(cwd), inner)
        return _run_capture(self._base() + ["bash", "-lc", shlex.quote(inner)], timeout=timeout)

    def run(self, command, timeout=RUN_TIMEOUT, env=None, run_dir=None):
        command = self._wrap_env(command, env)
        if run_dir:
            command = self._tracked_command(command, run_dir)
        return self._split(self._remote(command, timeout=timeout))

    def cleanup(self, run_dir):
        # Same reasoning as docker: the remote process outlives the ssh client,
        # so kill the recorded process group on the far side.
        try:
            self._remote(self._kill_group_script(run_dir), timeout=60.0)
            leftovers = self._remote(self._leftover_check_script(run_dir), timeout=30.0)
        except TransportError as exc:
            return "cleanup failed: %s" % exc
        remaining = _text(leftovers.stdout).strip()
        if remaining:
            return ("killed process group, but processes matching %s remain on %s: %s"
                    % (run_dir, self.host, remaining.replace("\n", "; ")[:200]))
        return "killed process group for %s on %s (no leftovers)" % (run_dir, self.host)


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

    def run_command(self, command, cwd=None, env=None, timeout=COMMAND_TIMEOUT):
        return run_on_host(command, cwd=cwd, env=env, timeout=timeout)

    def run(self, command, timeout=RUN_TIMEOUT, env=None, run_dir=None):
        command = self._wrap_env(command, env)
        tracked = self._tracked_command(command, run_dir) if run_dir else command
        # start_new_session mirrors the setsid used for docker/ssh: the shell and
        # everything it forks share one process group, so a timeout can kill the
        # whole tree instead of orphaning the simulator.
        try:
            proc = subprocess.Popen(["bash", "-lc", tracked],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            stdout, stderr = proc.communicate()
            raise TransportTimeout("command timed out after %.0fs: %s" % (timeout, command))
        return {"rc": proc.returncode, "stdout": _text(stdout), "stderr": _text(stderr)}

    @staticmethod
    def _kill_group(proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                proc.kill()
            except OSError:
                pass

    def cleanup(self, run_dir):
        self._kill_recorded_group(run_dir)
        remaining = self._leftovers(run_dir)
        if remaining:
            return ("killed process group, but processes matching %s remain on this machine: %s"
                    % (run_dir, remaining.replace("\n", "; ")[:200]))
        return "killed process group for %s on this machine (no leftovers)" % run_dir

    def _kill_recorded_group(self, run_dir):
        pidfile = self._pidfile(run_dir)
        try:
            with open(pidfile, encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError):
            pid = None
        if pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except OSError:
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                time.sleep(0.5)
        try:
            subprocess.run(["bash", "-lc", "pkill -f %s >/dev/null 2>&1 || true"
                            % shlex.quote(run_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.unlink(pidfile)
        except OSError:
            pass

    def _leftovers(self, run_dir):
        try:
            proc = subprocess.run(["bash", "-lc", self._leftover_check_script(run_dir)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30, text=True)
            return (proc.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return ""


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
