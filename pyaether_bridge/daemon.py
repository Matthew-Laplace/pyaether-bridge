#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-side resident daemon for pyaether-bridge.

Serves NDJSON requests/responses on a unix socket and keeps one persistent
container session (docker / ssh / local + the target-side
``session_bridge.py``). The session starts **lazily**: only the first ``exec``
really runs `docker exec`, so no PY_AETHER license seat is taken needlessly.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from collections import deque

from . import config, transports

DEFAULT_START_TIMEOUT = 240.0
PROTOCOL_SKEW = 30.0  # daemon waits this much longer than the container-side exec timeout
DOCKER_TIMEOUT = 20.0
PROBE_TIMEOUT = 20.0
MAX_FETCH_FILES = 20
MAX_FETCH_BYTES = 32 * 1024 * 1024
FETCH_SUFFIXES = (".raw", ".csv", ".measure", ".mt0", ".log", ".psf", ".txt")
LOG_TAIL_CHARS = 4000


class BridgeError(RuntimeError):
    """Expected runtime failure on the daemon side."""


def _log(message):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.DAEMON_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(line, flush=True)


def tail(path, limit=2000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[-limit:].strip()
    except OSError:
        return ""


def transport_info():
    """Static description of the current transport (docker / ssh / local); raises BridgeError on invalid config."""
    try:
        return transports.build()
    except transports.TransportError as exc:
        raise BridgeError(str(exc))


def probe_target(timeout=None):
    """Target reachability + interpreter information; raises BridgeError on failure."""
    target = transport_info()
    try:
        result = target.probe()
    except transports.TransportError as exc:
        raise BridgeError(str(exc))
    result["transport"] = target.label()
    return result


# Aether installs are named Aether_<version>...; the interpreter path a probe
# returns carries that name, so the version can be read without another call.
_VERSION_RE = re.compile(r"Aether_([0-9][^/]*)")


def observed_identity():
    """What the target reports it is, using the probe (plus one SSH hostname call).

    Everything here is *observed*; comparing it with the configured
    ``expected_*`` values is :func:`identity_report`'s job.
    """
    target = transport_info()
    try:
        info = target.probe()
    except transports.TransportError as exc:
        raise BridgeError(str(exc))
    observed = {
        "hostname": "",
        "container": "",
        "image": "",
        "aether_version": "",
        "license_server": config.LICENSE_SERVER or "",
        "transport": target.label(),
        "reachable": bool(info.get("reachable")),
        "python": info.get("python") or "",
        "detail": info.get("detail") or "",
    }
    if config.TRANSPORT == "docker":
        entry = (info.get("targets") or {}).get(config.CONTAINER) or {}
        observed["container"] = config.CONTAINER
        observed["image"] = entry.get("image") or ""
    elif config.TRANSPORT == "ssh":
        observed["hostname"] = config.SSH_HOST or ""
        try:
            result = target.run_command("hostname", timeout=15.0)
            name = str((result or {}).get("stdout") or "").strip()
            if name:
                observed["hostname"] = name
        except (transports.TransportError, KeyError, AttributeError):
            pass
    else:
        observed["hostname"] = socket.gethostname()
    match = _VERSION_RE.search(observed["python"])
    if match:
        observed["aether_version"] = match.group(1)
    return observed


def identity_report():
    """Compare the configured expectations with what the target reports.

    A *declared* expectation that cannot be observed is a failure, not a pass:
    the point of the check is to catch a wrong target, and "could not verify" is
    exactly the state in which a wrong target would slip through.
    """
    observed = {}
    error = ""
    try:
        observed = observed_identity()
    except BridgeError as exc:
        error = str(exc)
    checks = []
    for field in config.IDENTITY_FIELDS:
        expected = str(config.EXPECTED_IDENTITY.get(field) or "")
        got = str(observed.get(field) or "")
        if not expected:
            state = "unconfigured"
        elif not got:
            state = "unverifiable"
        elif expected == got:
            state = "match"
        else:
            state = "mismatch"
        checks.append({"field": field, "expected": expected, "observed": got,
                       "state": state})
    blocking = [check for check in checks if check["state"] in ("mismatch", "unverifiable")]
    return {
        "ok": not blocking and not error,
        "identity_configured": config.EXPECTED_IDENTITY_ACTIVE,
        "checks": checks,
        "blocking": blocking,
        "observed": observed,
        "error": error,
        "override": config.ALLOW_IDENTITY_MISMATCH,
        "fingerprint": config.TARGET_FINGERPRINT,
        "profile": config.PROFILE,
        "profile_source": config.PROFILE_SOURCE,
    }


def identity_error_text(report):
    """One actionable sentence for a failed identity check."""
    parts = []
    for check in report["blocking"]:
        if check["state"] == "mismatch":
            parts.append("%s is %r but %r was expected"
                         % (check["field"], check["observed"], check["expected"]))
        else:
            parts.append("%s should be %r but the target did not report it"
                         % (check["field"], check["expected"]))
    detail = "; ".join(parts) or report.get("error") or "identity check failed"
    return ("target identity check failed for profile %r: %s. Point the profile at "
            "the right target, or set PYAETHER_ALLOW_IDENTITY_MISMATCH=1 if this "
            "cross-target use is intentional." % (config.PROFILE, detail))


class LineReader:
    """fd-based line reader; select() provides timeout control independent of buffering layers."""

    def __init__(self, fileobj):
        self.fd = fileobj.fileno()
        self.buffer = bytearray()

    def readline(self, deadline=None):
        while True:
            index = self.buffer.find(b"\n")
            if index >= 0:
                line = bytes(self.buffer[:index])
                del self.buffer[: index + 1]
                return line.decode("utf-8", "replace").strip()
            remaining = None
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError("container session closed")
            self.buffer.extend(chunk)


_script_cache = {"stat": None, "bytes": None}


def _read_script():
    source = config.SESSION_SCRIPT_SRC
    stat = source.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    if _script_cache["stat"] != key:
        _script_cache["stat"] = key
        _script_cache["bytes"] = source.read_bytes()
    return _script_cache["bytes"]


def new_run_id(engine_name):
    """Run identifier: sortable timestamp + engine + 4 random hex characters."""
    return "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), engine_name, os.urandom(2).hex())


def netlist_filename(name, dialect):
    """Sanitise the netlist file name and give it the dialect's usual suffix."""
    suffix = {"spectre": ".scs", "empyrean": ".scs"}.get(dialect, ".cir")
    base = os.path.basename(name or "").strip() or "design"
    base = re.sub(r"[^A-Za-z0-9_.+-]", "_", base)
    if base.endswith(".scs") or base.endswith(".cir") or base.endswith(".sp") or base.endswith(".spi"):
        return base
    return base + suffix


def parse_find_listing(text):
    """Parse ``find . -maxdepth N -type f -printf '%p\\t%s\\n'`` output."""
    entries = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name = parts[0].strip().lstrip("./")
        if not name:
            continue
        try:
            size = int(parts[1])
        except ValueError:
            size = None
        entries.append({"name": name, "size": size})
    return entries


def normalize_design(value):
    """Accept ``"lib/cell/view"`` or a 3-item list; return a tuple or None."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        parts = [str(item).strip() for item in value]
        return tuple(parts) if all(parts) else None
    if isinstance(value, str) and value.count("/") == 2:
        parts = [item.strip() for item in value.split("/")]
        return tuple(parts) if all(parts) else None
    return None


def _fetch_wanted(entry, log_name, raw_name, fetch_results):
    """Decide whether one file of the run directory should be copied back."""
    name = entry["name"]
    if "/" in name:  # keep it to depth 1; result directories stay on the target
        return False
    if name == log_name:
        return True
    if not fetch_results:
        return False
    if entry.get("size") and entry["size"] > MAX_FETCH_BYTES:
        return False
    return name == raw_name or name.lower().endswith(FETCH_SUFFIXES)


class Session:
    """One persistent pyAether stdio session plus its metadata (the transport decides the target)."""

    def __init__(self):
        self.transport = transport_info()
        self.proc = None
        self.reader = None
        self.ready = False
        self.generation = 0
        self.import_seconds = None
        self.import_log = ""
        self.symbols = 0
        self.python = None
        self.remote_pid = None
        self.started_at = None
        self.stderr_lines = deque(maxlen=200)
        self.startup_noise = []
        self.last_error = None
        self._lock = threading.RLock()
        self._request_id = 0

    # ---- lifecycle ------------------------------------------------------
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def deploy(self):
        data = _read_script()
        try:
            self.transport.write_file(self.transport.script_path, data)
        except transports.TransportError as exc:
            raise BridgeError("failed to deploy the session script to %s: %s" % (self.transport.label(), exc))

    def kill(self, reason=""):
        proc = self.proc
        self.proc = None
        self.reader = None
        self.ready = False
        if proc is not None:
            # The child process must be killed before the pipes are closed: the
            # stderr drain thread may be blocked in readline() while holding the
            # buffer lock, so closing first would block forever (this once kept
            # the process alive even on SIGTERM).
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            for closer in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if closer is not None:
                        closer.close()
                except (OSError, ValueError):
                    pass
        if reason:
            self.last_error = reason
            _log("session terminated: %s" % reason)

    def _drain_stderr(self, pipe):
        try:
            for raw in iter(pipe.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self.stderr_lines.append(text)
        except (OSError, ValueError):
            pass

    def start(self, timeout=DEFAULT_START_TIMEOUT):
        with self._lock:
            self.kill()
            self.deploy()
            try:
                proc = self.transport.open_session(self.transport.session_command())
            except transports.TransportError as exc:
                raise BridgeError("failed to start the %s session: %s" % (self.transport.label(), exc))
            threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True).start()
            self.proc = proc
            self.reader = LineReader(proc.stdout)
            self.stderr_lines.clear()
            self.startup_noise = []
            deadline = time.time() + timeout
            while True:
                if proc.poll() is not None:
                    raise BridgeError(
                        "container session exited right after start (rc=%s): %s"
                        % (proc.returncode, " | ".join(list(self.stderr_lines)[-5:]))
                    )
                try:
                    line = self.reader.readline(deadline)
                except EOFError:
                    line = None
                if line is None:
                    self.kill("timed out waiting for the container session to become ready")
                    raise BridgeError(
                        "timed out waiting for the container session to become ready (%.0fs): %s"
                        % (timeout, " | ".join(list(self.stderr_lines)[-5:]))
                    )
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if isinstance(message, dict) and message.get("type") == "ready":
                    self.ready = bool(message.get("ok"))
                    self.generation += 1
                    self.import_seconds = message.get("import_seconds")
                    self.import_log = message.get("import_log") or ""
                    self.symbols = message.get("symbols") or 0
                    self.python = message.get("python")
                    self.remote_pid = message.get("pid")
                    self.started_at = time.time()
                    self.last_error = None if self.ready else self.import_log
                    return self.info()
                if line:
                    self.startup_noise.append(line)

    # ---- requests -------------------------------------------------------
    def call(self, method, params=None, timeout=30.0):
        with self._lock:
            if not self.alive():
                self.start()
            self._request_id += 1
            request_id = self._request_id
            message = json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False,
            )
            try:
                self.proc.stdin.write((message + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except OSError as exc:
                self.kill("failed to write to the container session: %s" % exc)
                raise BridgeError("failed to write to the container session: %s" % exc)
            deadline = time.time() + timeout
            while True:
                try:
                    line = self.reader.readline(deadline)
                except EOFError:
                    line = None
                if line is None:
                    self.kill("container session unresponsive for %.0fs" % timeout)
                    raise BridgeError(
                        "container session unresponsive for %.0fs (terminated; the next call restarts it)" % timeout
                    )
                try:
                    response = json.loads(line)
                except ValueError:
                    continue  # noise line (e.g. output from the login profile)
                if isinstance(response, dict) and response.get("id") == request_id:
                    return response

    def info(self):
        uptime = None
        if self.started_at is not None and self.alive():
            uptime = round(time.time() - self.started_at, 1)
        return {
            "ready": bool(self.ready and self.alive()),
            "started": self.started_at is not None,
            "alive": self.alive(),
            "generation": self.generation,
            "import_seconds": self.import_seconds,
            "import_log": self.import_log,
            "symbols": self.symbols,
            "python": self.python,
            "remote_pid": self.remote_pid,
            "transport": self.transport.label(),
            "uptime_s": uptime,
            "startup_noise": self.startup_noise[-5:],
            "stderr_tail": list(self.stderr_lines)[-10:],
            "last_error": self.last_error,
        }


class BridgeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, socket_path):
        super().__init__(socket_path, _Handler)
        self.session = Session()
        self.started_at = time.time()
        self.last_error = None
        # One identity check per daemon: it runs before the first write into the
        # live session, and is not repeated per request.
        self.identity_checked = False

    # ---- method dispatch ------------------------------------------------
    def dispatch(self, request):
        method = request.get("method")
        params = request.get("params") or {}
        if method == "ping":
            return {"ok": True, "pong": True, "pid": os.getpid(), "generation": self.session.generation}
        if method == "status":
            return self.status()
        if method == "exec":
            return self.exec_(params)
        if method == "namespace":
            if not self.session.alive():
                return {"ok": False, "error": "container session is not running yet", "error_type": "BridgeNotStarted"}
            response = self.session.call("namespace", {}, timeout=30.0)
            return {"ok": True, "keys": response.get("keys", []), "count": response.get("count", 0)}
        if method == "restart":
            self.session.start()
            return {"ok": True, "session": self.session.info()}
        if method == "stop":
            threading.Thread(target=self._delayed_shutdown, daemon=True).start()
            return {"ok": True, "stopping": True, "pid": os.getpid()}
        return {"ok": False, "error": "unknown method %r" % (method,), "error_type": "MethodNotFound"}

    def _delayed_shutdown(self):
        time.sleep(0.2)
        try:
            self.shutdown()
        except Exception:
            pass

    def status(self):
        target = self.session.transport.describe()
        target["label"] = self.session.transport.label()
        try:
            target.update(probe_target())
        except BridgeError as exc:
            target["reachable"] = False
            target["detail"] = str(exc)
            self.last_error = str(exc)
        keys = []
        if self.session.alive():
            try:
                keys = self.session.call("namespace", {}, timeout=30.0).get("keys", [])
            except BridgeError as exc:
                self.last_error = str(exc)
        return {
            "ok": True,
            "transport": target,
            "daemon": {
                "running": True,
                "pid": os.getpid(),
                "socket": str(config.DAEMON_SOCK),
                "uptime_s": round(time.time() - self.started_at, 1),
                "log": str(config.DAEMON_LOG),
                "fingerprint": config.TARGET_FINGERPRINT,
            },
            "session": self.session.info(),
            "namespace_keys": keys,
            "last_error": self.last_error or self.session.last_error,
        }

    def exec_(self, params):
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            return {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "result_repr": None,
                "result_json": None,
                "elapsed_s": 0.0,
                "timed_out": False,
                "error": "parameter code must be a non-empty string",
                "error_type": "ValueError",
                "namespace_new": 0,
            }
        try:
            timeout = float(params.get("timeout") or 120.0)
        except (TypeError, ValueError):
            timeout = 120.0
        if timeout <= 0:
            timeout = 120.0
        # First write into the live session: assert the target is the one the
        # profile claims, instead of discovering it afterwards. Skipped entirely
        # when no expected_* value is configured, so it costs nothing by default.
        if not self.identity_checked:
            self.identity_checked = True
            if config.EXPECTED_IDENTITY_ACTIVE:
                report = identity_report()
                if not report["ok"] and not config.ALLOW_IDENTITY_MISMATCH:
                    self.last_error = identity_error_text(report)
                    return {
                        "ok": False,
                        "stdout": "",
                        "stderr": "",
                        "result_repr": None,
                        "result_json": None,
                        "elapsed_s": 0.0,
                        "timed_out": False,
                        "error": self.last_error,
                        "error_type": "IdentityMismatch",
                        "namespace_new": 0,
                        "identity": report,
                    }
        started = time.time()
        try:
            response = self.session.call(
                "exec", {"code": code, "timeout": timeout}, timeout=timeout + PROTOCOL_SKEW
            )
        except BridgeError as exc:
            self.last_error = str(exc)
            return {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "result_repr": None,
                "result_json": None,
                "elapsed_s": round(time.time() - started, 3),
                "timed_out": False,
                "error": str(exc),
                "error_type": "BridgeError",
                "namespace_new": 0,
                "session": self.session.info(),
            }
        response.pop("id", None)
        response["ok"] = bool(response.get("ok"))
        self.last_error = None if response["ok"] else (response.get("error") or "")[:500] or None
        return response


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(120.0)
        try:
            raw = self.rfile.readline()
        except OSError:
            return
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            self._reply({"ok": False, "error": "invalid request: %s" % exc, "error_type": "ValueError"})
            return
        self.connection.settimeout(None)
        try:
            response = self.server.dispatch(request)
        except BridgeError as exc:
            response = {"ok": False, "error": str(exc), "error_type": "BridgeError"}
        except Exception:
            response = {
                "ok": False,
                "error": traceback.format_exc(limit=10),
                "error_type": "DaemonInternalError",
            }
        response.setdefault("id", request.get("id"))
        self._reply(response)

    def _reply(self, payload):
        # Every reply carries the target fingerprint: a client that resolved a
        # different target must not be served by this daemon, and checking on
        # each reply is cheaper and safer than trusting a stale on-disk marker.
        if isinstance(payload, dict):
            payload.setdefault("fingerprint", config.TARGET_FINGERPRINT)
        data = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            pass


def _lock_daemon():
    # One lock per profile, next to that profile's socket. A global lock would
    # let the first profile's daemon block every other profile's daemon from
    # starting, even though each profile has its own session and target.
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(config.DAEMON_LOCK, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def serve(start_timeout=DEFAULT_START_TIMEOUT):
    lock = _lock_daemon()
    if lock is None:
        _log("another daemon is already running for profile %r; this start exits"
             % config.PROFILE)
        return 0
    socket_path = config.DAEMON_SOCK
    if socket_path.exists():
        socket_path.unlink()
    server = BridgeServer(str(socket_path))
    os.chmod(str(socket_path), 0o600)
    try:
        with open(config.DAEMON_PID, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass
    _log("daemon started pid=%d socket=%s" % (os.getpid(), socket_path))

    def _shutdown(signum, frame):
        # Safety net: if the cleanup phase gets stuck again, exit unconditionally
        # after 15s instead of leaving a half-dead daemon behind (process alive,
        # socket closed, lock still held) that makes every later start fail.
        timer = threading.Timer(15.0, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.session.kill("daemon exiting")
        server.server_close()
        for path in (socket_path, config.DAEMON_PID):
            try:
                path.unlink()
            except OSError:
                pass
        _log("daemon exited pid=%d" % os.getpid())
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pyaether_bridge.daemon", description="pyaether-bridge host daemon")
    parser.add_argument("--serve", action="store_true", help="run the daemon in the foreground (default behavior)")
    parser.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT, help="maximum seconds to wait for the container session to become ready")
    args = parser.parse_args(argv)
    return serve(start_timeout=args.start_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
