#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pyaether-bridge target-side resident executor (docker / ssh / local),
started by the host daemon.

How it is started (a login shell is required: Aether's ``LD_LIBRARY_PATH``
lives in the profile)::

    bash -lc "exec python3.9 /tmp/pyaether-bridge/session_bridge.py"

Protocol: NDJSON on stdin/stdout, one JSON object per line::

    -> {"id": 1, "method": "exec", "params": {"code": "1+1", "timeout": 120}}
    <- {"id": 1, "ok": true, "stdout": "", "stderr": "", "result_repr": "2", ...}

Design note: the protocol actually rides on ``_PROTO_OUT_FD`` / ``_PROTO_IN_FD``,
duplicated at startup. The process's fd 0/1/2 are redirected before pyAether is
imported (to the startup log during import, to temp files while serving), so
Aether's banner, C-library output and any ``print`` from user code cannot
corrupt the protocol stream. The namespace stays resident: ``import pyAether``
(~8.5 s, ~16k public names) happens exactly once.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import signal
import sys
import tempfile
import time
import traceback

MAX_STREAM_CHARS = 200_000
MAX_REPR_CHARS = 20_000
MAX_JSON_CHARS = 200_000
MAX_IMPORT_LOG_CHARS = 4_000

_PROTO_OUT_FD = os.dup(1)
_PROTO_IN_FD = os.dup(0)
_DEVNULL_FD = os.open(os.devnull, os.O_RDWR)
os.dup2(_DEVNULL_FD, 0)  # user code reading stdin gets EOF, never a protocol request

_requests = os.fdopen(_PROTO_IN_FD, "r", encoding="utf-8", errors="replace")

namespace = {
    "__name__": "__main__",
    "__doc__": None,
    "__builtins__": builtins,
}


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _write_message(obj):
    data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    try:
        _write_all(_PROTO_OUT_FD, data)
    except OSError:
        os._exit(1)  # the host is already gone


def _redirect_fds(target_fd):
    """Point both fd 1 and fd 2 at target_fd, flushing Python's own buffers first."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os.dup2(target_fd, 1)
    os.dup2(target_fd, 2)


def _capture_start():
    """Start capturing fd-level output; returns (stdout file, stderr file)."""
    out = tempfile.TemporaryFile()
    err = tempfile.TemporaryFile()
    _redirect_fds(out.fileno())
    os.dup2(err.fileno(), 2)
    return out, err


def _capture_stop(handles):
    out, err = handles
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    _redirect_fds(_DEVNULL_FD)
    texts = []
    for handle in (out, err):
        try:
            handle.seek(0)
            texts.append(handle.read().decode("utf-8", "replace"))
        finally:
            handle.close()
    return texts[0], texts[1]


def _truncate(text, limit):
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated %d characters]" % (len(text) - limit)


def _safe_repr(value):
    try:
        text = repr(value)
    except BaseException as exc:  # even a hostile __repr__ must not kill the session
        return "<repr failed: %s>" % type(exc).__name__
    return _truncate(text, MAX_REPR_CHARS)


def _safe_json(value):
    try:
        text = json.dumps(value, ensure_ascii=False)
    except BaseException:
        return None
    if len(text) > MAX_JSON_CHARS:
        return None
    return text


class _Timeout(Exception):
    pass


def _raise_timeout(signum, frame):
    raise _Timeout("execution exceeded %.1fs and was interrupted" % _timeout_seconds[0])


_timeout_seconds = [0.0]


def _exec_code(code, timeout):
    """Execute code in the resident namespace; the value of the final
    expression becomes the result."""
    payload = {
        "ok": True,
        "result_repr": None,
        "result_json": None,
        "timed_out": False,
        "error": None,
        "error_type": None,
        "namespace_new": 0,
    }
    before = set(namespace)
    use_alarm = bool(timeout) and timeout > 0 and hasattr(signal, "SIGALRM")
    previous = None
    if use_alarm:
        _timeout_seconds[0] = float(timeout)
        previous = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, float(timeout))
    try:
        tree = ast.parse(code, "<pyaether>", "exec")
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            head = ast.Module(body=tree.body[:-1], type_ignores=[])
            if head.body:
                exec(compile(head, "<pyaether>", "exec"), namespace)
            tail = ast.Expression(body=tree.body[-1].value)
            ast.fix_missing_locations(tail)
            result = eval(compile(tail, "<pyaether>", "eval"), namespace)
        else:
            exec(compile(tree, "<pyaether>", "exec"), namespace)
            result = None
        payload["result_repr"] = _safe_repr(result)
        payload["result_json"] = _safe_json(result)
    except _Timeout as exc:
        payload["ok"] = False
        payload["timed_out"] = True
        payload["error"] = str(exc)
        payload["error_type"] = "TimeoutError"
    except SystemExit as exc:
        payload["ok"] = False
        payload["error"] = "SystemExit(%r)" % (exc.code,)
        payload["error_type"] = "SystemExit"
    except BaseException as exc:
        payload["ok"] = False
        payload["error"] = traceback.format_exc(limit=15)
        payload["error_type"] = type(exc).__name__
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    payload["namespace_new"] = len(set(namespace) - before)
    return payload


def _handle(request):
    method = request.get("method")
    params = request.get("params") or {}
    if method == "ping":
        return {"pong": True}
    if method == "info":
        return {
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "namespace_keys": sorted(namespace),
            "pyAether": "pyAether" in namespace,
        }
    if method == "namespace":
        return {"keys": sorted(namespace), "count": len(namespace)}
    if method == "exec":
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "argument code must be a non-empty string",
                    "error_type": "ValueError"}
        try:
            timeout = float(params.get("timeout") or 120)
        except (TypeError, ValueError):
            return {"ok": False, "error": "argument timeout must be a number",
                    "error_type": "ValueError"}
        handles = _capture_start()
        started = time.time()
        try:
            result = _exec_code(code, timeout)
        finally:
            stdout_text, stderr_text = _capture_stop(handles)
        result["stdout"] = _truncate(stdout_text, MAX_STREAM_CHARS)
        result["stderr"] = _truncate(stderr_text, MAX_STREAM_CHARS)
        result["elapsed_s"] = round(time.time() - started, 3)
        return result
    return {"ok": False, "error": "unknown method %r" % (method,),
            "error_type": "MethodNotFound"}


def main():
    handles = _capture_start()
    started = time.time()
    ok = True
    import_log = ""
    module = None
    try:
        import pyAether  # noqa: F401  -- the module name is lowercase pyAether

        module = pyAether
        pyAether.emyInitDb()
        namespace["pyAether"] = pyAether
    except BaseException:
        ok = False
        import_log = traceback.format_exc(limit=15)
    stdout_text, stderr_text = _capture_stop(handles)
    if not import_log:
        import_log = _truncate((stdout_text + stderr_text).strip(), MAX_IMPORT_LOG_CHARS)
    symbols = 0
    if ok and module is not None:
        symbols = len([n for n in dir(module) if not n.startswith("_")])
    _write_message(
        {
            "type": "ready",
            "ok": ok,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "import_seconds": round(time.time() - started, 3),
            "symbols": symbols,
            "import_log": import_log,
        }
    )

    while True:
        line = _requests.readline()
        if not line:  # the host closed the pipe
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("a request must be a JSON object")
        except BaseException as exc:
            _write_message({"id": None, "ok": False,
                            "error": "invalid request: %s" % exc,
                            "error_type": "ValueError"})
            continue
        if request.get("method") in ("exit", "shutdown"):
            _write_message({"id": request.get("id"), "ok": True, "bye": True})
            return 0
        try:
            response = _handle(request)
        except BaseException:
            response = {"ok": False, "error": traceback.format_exc(limit=15), "error_type": "BridgeInternalError"}
        response["id"] = request.get("id")
        _write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
