#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-side Python client: unix socket -> pyaether daemon.

The CLI and the MCP server share this entry point. When the daemon is not
running it is started automatically (disable with
``PYAETHER_BRIDGE_NO_AUTOSTART=1``); a failed connection is retried once, but
only when **no request was sent yet**, so ``exec_code`` is never silently replayed.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time

from . import config


class BridgeError(RuntimeError):
    """The daemon was reached but the operation failed (protocol error, crashed session, ...)."""


class BridgeNotRunning(BridgeError):
    """The daemon is not running: the unix socket cannot be connected."""


def _readline(sock, deadline):
    buffer = bytearray()
    while True:
        index = buffer.find(b"\n")
        if index >= 0:
            return bytes(buffer[:index]).decode("utf-8", "replace")
        remaining = deadline - time.time()
        if remaining <= 0:
            raise BridgeError("timed out waiting for a daemon response")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise BridgeError("timed out waiting for a daemon response")
        if not chunk:
            raise BridgeError("daemon closed the connection before returning a response")
        buffer.extend(chunk)


def _call_once(method, params, timeout):
    payload = (
        json.dumps({"id": 1, "method": method, "params": params or {}}, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.time() + timeout
    try:
        sock.settimeout(min(timeout, 10.0))
        try:
            sock.connect(str(config.DAEMON_SOCK))
        except (FileNotFoundError, NotADirectoryError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise BridgeNotRunning("daemon is not running (%s): %s" % (config.DAEMON_SOCK, exc))
        sock.sendall(payload)
        line = _readline(sock, deadline)
    finally:
        sock.close()
    try:
        response = json.loads(line)
    except ValueError:
        raise BridgeError("daemon returned invalid JSON: %r" % line[:200])
    if not isinstance(response, dict):
        raise BridgeError("daemon returned an unexpected JSON type: %s" % type(response).__name__)
    return response


def daemon_pid():
    try:
        return int(config.DAEMON_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _spawn_daemon():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(config.DAEMON_LOG, "ab")
    root = str(config.PROJECT_DIR)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "pyaether_bridge.daemon", "--serve"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            env=env,
            start_new_session=True,
        )
    finally:
        handle.close()


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _clear_wedged_daemon():
    """Clear a wedged daemon: the process is still alive (holding the flock) but
    its socket can no longer be connected.

    This usually stems from a cleanup that got stuck during the previous
    shutdown; left alone it makes every later start fail with "another daemon
    is already running". Only the process recorded in this project's pid file is
    touched.
    """
    pid = daemon_pid()
    if not _pid_alive(pid):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    deadline = time.time() + 8.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.5)
    for path in (config.DAEMON_SOCK, config.DAEMON_PID):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    return pid


def ensure_daemon(*, wait=90.0):
    """Make sure the daemon is running; returns {'running','started','pid','socket'}."""
    try:
        response = _call_once("ping", {}, timeout=3.0)
        if response.get("pong"):
            return {"running": True, "started": False, "pid": response.get("pid") or daemon_pid(), "socket": str(config.DAEMON_SOCK)}
    except BridgeNotRunning:
        _clear_wedged_daemon()
    except BridgeError:
        pass
    process = _spawn_daemon()
    deadline = time.time() + wait
    last_error = None
    while time.time() < deadline:
        try:
            response = _call_once("ping", {}, timeout=3.0)
            if response.get("pong"):
                return {
                    "running": True,
                    "started": True,
                    "pid": response.get("pid") or process.pid,
                    "socket": str(config.DAEMON_SOCK),
                }
        except BridgeNotRunning as exc:
            last_error = exc
        except BridgeError as exc:
            last_error = exc
        if process.poll() is not None and not config.DAEMON_SOCK.exists():
            raise BridgeError(
                "daemon exited right after start (rc=%s); log tail:\n%s" % (process.returncode, tail_log())
            )
        time.sleep(0.2)
    raise BridgeError("daemon was not ready within %.0fs (%s); log tail:\n%s" % (wait, last_error, tail_log()))


def tail_log(limit=2000):
    try:
        with open(config.DAEMON_LOG, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[-limit:].strip()
    except OSError:
        return ""


def stop_daemon():
    """Stop the daemon; returns {'running','stopped',...}. A daemon that is not running is not an error."""
    try:
        _call_once("stop", {}, timeout=10.0)
    except BridgeNotRunning:
        return {"running": False, "stopped": False, "reason": "daemon is not running"}
    except BridgeError as exc:
        return {"running": True, "stopped": False, "reason": str(exc)}
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            _call_once("ping", {}, timeout=1.0)
        except BridgeNotRunning:
            return {"running": False, "stopped": True}
        except BridgeError:
            pass
        time.sleep(0.1)
    killed = _clear_wedged_daemon()
    return {
        "running": False if killed else True,
        "stopped": bool(killed),
        "reason": "did not exit cleanly within 15s; force-killed pid=%s" % killed if killed
                  else "did not exit within 15s",
    }


def request(method, params=None, *, timeout=30.0, autostart=True):
    """Send one method call to the daemon and return the raw response dict."""
    allow_start = bool(autostart) and not config.NO_AUTOSTART
    try:
        return _call_once(method, params, timeout)
    except BridgeNotRunning:
        if not allow_start:
            raise
    ensure_daemon()
    return _call_once(method, params, timeout)


def exec_code(code, *, timeout=120.0, autostart=True):
    """Execute Python code inside the container's persistent namespace.

    Returns {"ok","stdout","stderr","result_repr","result_json","elapsed_s",
    "timed_out","error","error_type","namespace_new"}. When the user code
    raises, ``ok=False`` but the call itself still succeeds (no Python exception
    escapes).
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    timeout = float(timeout or 120.0)
    response = request("exec", {"code": code, "timeout": timeout}, timeout=timeout + 60.0, autostart=autostart)
    if "ok" not in response:
        raise BridgeError("daemon response is missing the ok field: %r" % (response,))
    return response


def _offline_status(reason):
    target = {"label": str(config.TRANSPORT), "kind": str(config.TRANSPORT),
              "reachable": False}
    probe_error = None
    try:
        from . import daemon as daemon_module

        target.update(daemon_module.transport_info().describe())
        target["label"] = daemon_module.transport_info().label()
        target.update(daemon_module.probe_target())
    except Exception as exc:  # must still answer even when the target is unreachable
        probe_error = str(exc)
        target["detail"] = probe_error
    return {
        "transport": target,
        "daemon": {
            "running": False,
            "pid": None,
            "socket": str(config.DAEMON_SOCK),
            "uptime_s": None,
            "log": str(config.DAEMON_LOG),
        },
        "session": {
            "ready": False,
            "started": False,
            "alive": False,
            "generation": 0,
            "import_seconds": None,
            "import_log": "",
            "symbols": 0,
            "uptime_s": None,
        },
        "namespace_keys": [],
        "last_error": probe_error or reason,
    }


def status(*, autostart=False):
    """Container + daemon + session status; reports an offline view when the daemon is not running and does not start a session."""
    try:
        response = request("status", {}, timeout=60.0, autostart=autostart)
    except BridgeNotRunning as exc:
        return _offline_status(str(exc))
    if not response.get("ok"):
        raise BridgeError(response.get("error") or "status call failed")
    return {
        "transport": response.get("transport", {}),
        "daemon": response.get("daemon", {}),
        "session": response.get("session", {}),
        "namespace_keys": response.get("namespace_keys", []),
        "last_error": response.get("last_error"),
    }


