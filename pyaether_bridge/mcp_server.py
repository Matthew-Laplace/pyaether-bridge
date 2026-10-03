#!/usr/bin/env python3
"""MCP stdio server for the PyAether bridge (standard library only).

Both entry points must work:

    python3 -m pyaether_bridge.mcp_server
    python3 /abs/PROJECT_DIR/pyaether_bridge/mcp_server.py

Transport: stdio, NDJSON, one JSON-RPC 2.0 message per line (MCP 2024-11-05).
stdout carries protocol messages only: at startup the original stdout is
duplicated for replies and fd 1 is pointed at stderr, so a stray print from any
library lands on stderr.
Imports at module level stay light; runtime/catalog are imported lazily on the
first tool call.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import sys
import traceback

MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pyaether-bridge"

INSTRUCTIONS = (
    "PyAether bridge server. Recommended order: pyaether_api_search to find an "
    "API, pyaether_api_help to read its signature and parameters, then "
    "pyaether_exec to run Python inside the resident PyAether session. The host "
    "daemon is started automatically (forbidden when "
    "PYAETHER_BRIDGE_NO_AUTOSTART=1); pyaether_status only reads state. "
    "pyaether_sim_run runs a netlist on a switchable simulator: open-source "
    "ngspice, Cadence Spectre/APS on the target, or a configured custom "
    "command. Treat the returned ok/status as the execution contract."
)

_MISSING = object()
_PROTOCOL_OUT = None


def _bootstrap_path():
    """Put the repository root on sys.path when this file is run directly
    (rather than via -m)."""
    if __package__ not in (None, ""):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bootstrap_path()

from pyaether_bridge import __version__  # noqa: E402  (must come after bootstrap)


def log(message):
    """Diagnostics go to stderr only, never onto the stdout protocol stream."""
    try:
        sys.stderr.write("[pyaether-mcp] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


_TOOL_HANDLERS = {}


class _BadArgument(ValueError):
    """Invalid argument (surfaces to the MCP client as isError text, not as a
    protocol error)."""


# --------------------------------------------------------------- tool schema

def _object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "pyaether_status",
        "description": "Show host daemon, target bridge and PyAether session status "
                        "(read-only, never starts the daemon).",
        "inputSchema": _object_schema({}),
    },
    {
        "name": "pyaether_api_search",
        "description": "Search the local PyAether API catalog; returns "
                        "name/kind/module/signature/summary.",
        "inputSchema": _object_schema(
            {
                "query": {
                    "type": "string",
                    "description": "Search term, for example emyInitDb, initialize, db.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Maximum number of hits, default 20.",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional kind filter, for example function / class / attribute.",
                },
            },
            ("query",),
        ),
    },
    {
        "name": "pyaether_api_help",
        "description": "Show one API signature, parameters and return value "
                        "(usually search first, then help).",
        "inputSchema": _object_schema(
            {
                "symbol": {
                    "type": "string",
                    "description": "Full API name, for example pyAether.emyInitDb.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 200000,
                    "default": 4000,
                    "description": "Description truncation length, default 4000.",
                },
            },
            ("symbol",),
        ),
    },
    {
        "name": "pyaether_exec",
        "description": "Run Python code inside the target PyAether session and "
                        "return stdout plus the result.",
        "inputSchema": _object_schema(
            {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute.",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 3600,
                    "default": 120,
                    "description": "Execution timeout in seconds, default 120, max 3600.",
                },
            },
            ("code",),
        ),
    },
    {
        "name": "pyaether_sim_run",
        "description": "Run a SPICE netlist on the chosen simulator (ngspice, "
                        "Spectre/APS, or a configured custom command) and return "
                        "parsed results. Check the returned ok/status before using data.",
        "inputSchema": _object_schema(
            {
                "netlist": {
                    "type": "string",
                    "description": "Netlist to run; sent to the target as text.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["ngspice", "spectre", "alps", "custom"],
                    "description": "Simulator backend; defaults to PYAETHER_SIM_BACKEND "
                                   "(ngspice).",
                },
                "mode": {
                    "type": "string",
                    "description": "Spectre engine/preset (aps, ax, mx, ...); ignored by ngspice.",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 86400,
                    "default": 600,
                    "description": "Simulation timeout in seconds, default 600.",
                },
            },
            ("netlist",),
        ),
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)


# ------------------------------------------------------------------- helpers

def _clamp_number(value, name, low, high, integer):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _BadArgument("argument %s must be a number" % name)
    if integer and float(value) != int(value):
        raise _BadArgument("argument %s must be an integer" % name)
    number = int(value) if integer else float(value)
    if number < low:
        number = low
    elif number > high:
        number = high
    return number


def _to_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, default=repr)


def _load(module_name):
    """Import runtime/catalog on demand so startup stays cheap."""
    return importlib.import_module("pyaether_bridge." + module_name)


def _format_item(item):
    if isinstance(item, dict):
        name = item.get("name") or "?"
        type_name = item.get("type") or item.get("type_name") or "?"
        desc = item.get("desc") or item.get("description") or ""
        text = "%s : %s" % (name, type_name)
        return text + (" — %s" % desc if desc else "")
    return str(item)


def _format_entry(entry):
    lines = []

    def add(label, value):
        if value not in (None, "", [], {}):
            lines.append("%s: %s" % (label, value))

    add("name", entry.get("name"))
    add("kind", entry.get("kind"))
    add("module", entry.get("module"))
    add("signature", entry.get("signature"))
    add("domain", entry.get("domain"))
    page = entry.get("page")
    anchor = entry.get("anchor")
    if page:
        add("page", "%s#%s" % (page, anchor) if anchor else page)
    add("summary", entry.get("summary"))
    description = entry.get("description")
    if description:
        lines.append("description:")
        lines.append(str(description))
    params = entry.get("params") or []
    if params:
        lines.append("parameters:")
        lines.extend("  - %s" % _format_item(item) for item in params)
    returns = entry.get("returns") or []
    if returns:
        lines.append("returns:")
        lines.extend("  - %s" % _format_item(item) for item in returns)
    return "\n".join(lines)


def _format_exec(result):
    lines = ["ok: %s" % bool(result.get("ok"))]
    for key in ("elapsed_s", "timed_out", "namespace_new", "error_type"):
        if key in result and result[key] is not None:
            lines.append("%s: %s" % (key, result[key]))
    stdout = result.get("stdout") or ""
    lines.append("stdout:")
    lines.append(stdout.rstrip("\n") if stdout.strip() else "(empty)")
    stderr = result.get("stderr") or ""
    if stderr.strip():
        lines.append("stderr:")
        lines.append(stderr.rstrip("\n"))
    result_repr = result.get("result_repr")
    if result_repr is None and result.get("result_json") is not None:
        result_repr = result.get("result_json")
    lines.append("result: %s" % ("" if result_repr is None else result_repr))
    error = result.get("error")
    if error:
        lines.append("error: %s" % error)
    return "\n".join(lines)


# ------------------------------------------------------------------- the tools

def _tool_status(arguments):
    runtime = _load("runtime")
    return _to_json(runtime.status()), False


def _tool_api_search(arguments):
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise _BadArgument("argument query must be a non-empty string")
    limit = _clamp_number(arguments.get("limit", 20), "limit", 1, 100, True)
    kind = arguments.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise _BadArgument("argument kind must be a string")
    catalog = _load("catalog")
    hits = catalog.search(query, limit=limit, kind=kind) or []
    if not hits:
        return "no matches: the API catalog has nothing for \"%s\"" % query, False
    return "%d hit(s) (limit=%d):\n%s" % (len(hits), limit, _to_json(list(hits))), False


def _tool_api_help(arguments):
    symbol = arguments.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise _BadArgument("argument symbol must be a non-empty string")
    max_chars = _clamp_number(arguments.get("max_chars", 4000), "max_chars", 200, 200000, True)
    catalog = _load("catalog")
    entry = catalog.show(symbol, max_chars=max_chars)
    if not entry:
        return (
            "no API named \"%s\". Use pyaether_api_search to look up the name, "
            "then pyaether_api_help for its signature." % symbol,
            True,
        )
    return _format_entry(entry), False


def _tool_exec(arguments):
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise _BadArgument("argument code must be a non-empty string")
    timeout = _clamp_number(arguments.get("timeout", 120), "timeout", 0.1, 3600, False)
    runtime = _load("runtime")
    result = runtime.exec_code(code, timeout=timeout)
    if not isinstance(result, dict):
        return "execution returned a non-dict result: %r" % (result,), False
    return _format_exec(result), not bool(result.get("ok"))


def _tool_sim_run(arguments):
    netlist = arguments.get("netlist")
    if not isinstance(netlist, str) or not netlist.strip():
        raise _BadArgument("argument netlist must be a non-empty string")
    backend = arguments.get("backend")
    if backend is not None and not isinstance(backend, str):
        raise _BadArgument("argument backend must be a string")
    mode = arguments.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise _BadArgument("argument mode must be a string")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    simulator = _load("simulators")
    name = "netlist-" + hashlib.sha1(netlist.encode("utf-8")).hexdigest()[:8]
    extension = ".cir" if (backend or "ngspice") == "ngspice" else ".scs"
    try:
        result = simulator.run(name + extension, backend=backend, mode=mode,
                               timeout=timeout, netlist_text=netlist)
    except simulator.SimulatorError as exc:
        return "simulation could not start: %s" % exc, True
    if not isinstance(result, dict):
        return "simulation returned a non-dict result: %r" % (result,), True
    return _format_sim(result), not bool(result.get("ok"))


def _format_sim(result):
    """Compact, honest rendering: status first, then numbers, then causes."""
    lines = [
        "status: %s (ok=%s)" % (result.get("status"), result.get("ok")),
        "backend: %s" % result.get("backend"),
    ]
    metadata = result.get("metadata") or {}
    if metadata.get("target"):
        lines.append("target: %s" % metadata["target"])
    if metadata.get("mode"):
        lines.append("mode: %s" % metadata["mode"])
    data = result.get("data") or {}
    if data:
        summary = []
        for key, value in data.items():
            if isinstance(value, list):
                summary.append("%s[%d]" % (key, len(value)))
            else:
                summary.append("%s=%s" % (key, value))
        lines.append("data: " + ", ".join(summary))
    else:
        lines.append("data: (none parsed)")
    for key in ("plots", "artifacts"):
        if metadata.get(key):
            lines.append("%s: %s" % (key, json.dumps(metadata[key], ensure_ascii=False)[:400]))
    if metadata.get("work_dir"):
        lines.append("work_dir: %s" % metadata["work_dir"])
    timings = metadata.get("timings") or {}
    if timings:
        lines.append("timings: " + json.dumps(timings))
    for error in result.get("errors") or []:
        lines.append("error: %s" % error)
    for warning in (result.get("warnings") or [])[:5]:
        lines.append("warning: %s" % warning)
    return "\n".join(lines)


_TOOL_HANDLERS.update(
    {
        "pyaether_status": _tool_status,
        "pyaether_api_search": _tool_api_search,
        "pyaether_api_help": _tool_api_help,
        "pyaether_exec": _tool_exec,
        "pyaether_sim_run": _tool_sim_run,
    }
)


# --------------------------------------------------------------- JSON-RPC layer

def _error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result_response(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _handle_initialize(params):
    protocol = params.get("protocolVersion")
    if not isinstance(protocol, str) or not protocol.strip():
        protocol = DEFAULT_PROTOCOL_VERSION
    client = params.get("clientInfo")
    client_name = client.get("name") if isinstance(client, dict) else client
    log("initialize: protocolVersion=%s clientInfo=%s" % (protocol, client_name))
    return {
        "protocolVersion": protocol,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def _handle_tools_call(params):
    """Return (error_dict | None, result | None); error_dict doubles as the
    protocol error reply (-32602 and friends)."""
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"code": -32602, "message": "invalid params: tools/call is missing name"}, None
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return (
            {
                "code": -32602,
                "message": "unknown tool: %s (available: %s)" % (name, ", ".join(TOOL_NAMES)),
            },
            None,
        )
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"code": -32602, "message": "invalid params: tools/call arguments must be an object"}, None
    try:
        text, is_error = handler(arguments)
    except _BadArgument as exc:
        text, is_error = str(exc), True
    except Exception as exc:  # a failing tool must never kill the server
        log("tool %s raised:\n%s" % (name, traceback.format_exc()))
        text = "tool %s failed: %s: %s" % (name, type(exc).__name__, exc)
        is_error = True
    return None, {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


def handle_message(message):
    """Handle one JSON-RPC message; returns a response dict, or None for a
    notification."""
    if not isinstance(message, dict):
        return _error_response(None, -32600, "invalid request: a JSON-RPC message must be an object")
    request_id = message.get("id", _MISSING)
    is_notification = request_id is _MISSING
    method = message.get("method")
    if not isinstance(method, str):
        if is_notification:
            return None
        return _error_response(request_id, -32600, "invalid request: missing method")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error_response(request_id, -32602, "invalid params: params must be an object")

    if method.startswith("notifications/"):
        log("notification: %s" % method)
        return None

    if method == "initialize":
        return _result_response(request_id, _handle_initialize(params))
    if method == "ping":
        return _result_response(request_id, {})
    if method == "tools/list":
        return _result_response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        error, result = _handle_tools_call(params)
        if is_notification:
            return None
        if error is not None:
            return _error_response(request_id, error["code"], error["message"])
        return _result_response(request_id, result)

    if is_notification:
        log("ignoring unknown notification: %s" % method)
        return None
    return _error_response(request_id, -32601, "unknown method: %s" % method)


# ------------------------------------------------------------------ io loop

def _iter_lines(stream, limit=MAX_LINE_BYTES):
    """Read a binary stream line by line; a line longer than limit yields None
    (that line is dropped)."""
    while True:
        chunk = stream.readline(limit + 1)
        if not chunk:
            return
        if len(chunk) > limit:
            if not chunk.endswith(b"\n"):
                while True:
                    tail = stream.readline(limit + 1)
                    if not tail or tail.endswith(b"\n"):
                        break
            yield None
            continue
        yield chunk


def _emit(stream, payload):
    stream.write(json.dumps(payload, ensure_ascii=False))
    stream.write("\n")
    stream.flush()


def serve(stdin=None, stdout=None):
    """Main loop: read one line, answer one line; exits cleanly on EOF."""
    if stdin is None:
        stdin = getattr(sys.stdin, "buffer", None) or sys.stdin
    if stdout is None:
        stdout = _PROTOCOL_OUT if _PROTOCOL_OUT is not None else sys.stdout

    for raw in _iter_lines(stdin):
        if raw is None:
            _emit(
                stdout,
                _error_response(
                    None, -32700, "message too long: line exceeded %d bytes and was dropped" % MAX_LINE_BYTES
                ),
            )
            continue
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except Exception as exc:
            _emit(stdout, _error_response(None, -32700, "JSON parse error: %s" % exc))
            continue
        try:
            response = handle_message(message)
        except Exception:
            log("handler crashed:\n%s" % traceback.format_exc())
            request_id = message.get("id") if isinstance(message, dict) else None
            response = _error_response(request_id, -32603, "internal server error")
        if response is not None:
            _emit(stdout, response)


def _install_output_guard():
    """Duplicate stdout for protocol use and point fd 1 at stderr, keeping
    stdout clean."""
    global _PROTOCOL_OUT
    try:
        stream = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n", buffering=1)
    except OSError:
        _PROTOCOL_OUT = sys.stdout
        return
    _PROTOCOL_OUT = stream
    try:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    except OSError:
        pass


def main(argv=None):
    _install_output_guard()
    log("started: version=%s pid=%d python=%s" % (__version__, os.getpid(), sys.version.split()[0]))
    try:
        serve()
    except KeyboardInterrupt:
        log("interrupted")
    except BrokenPipeError:
        pass
    finally:
        try:
            if _PROTOCOL_OUT is not None:
                _PROTOCOL_OUT.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
