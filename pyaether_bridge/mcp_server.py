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
    {
        "name": "pyaether_layout_gen",
        "description": "Build a GDS2/OASIS layout with KLayout from a JSON spec "
                        "(cells, boxes, polygons, paths, texts, instance arrays). "
                        "Returns the written path, shape counts and extents.",
        "inputSchema": _object_schema(
            {
                "spec": {
                    "type": "object",
                    "description": "Layout spec: {top, dbu, layers: {name: [layer, "
                                   "datatype]}, cells: {name: {shapes: [...], "
                                   "instances: [...]}}}. Lengths are microns unless "
                                   "the spec sets unit.",
                },
                "output": {
                    "type": "string",
                    "description": "Output path; .gds or .oas decides the format. "
                                   "Defaults to a file in the working directory.",
                },
                "timeout": {
                    "type": "number", "minimum": 1, "maximum": 86400, "default": 600,
                    "description": "Timeout in seconds, default 600.",
                },
            },
            ("spec",),
        ),
    },
    {
        "name": "pyaether_layout_info",
        "description": "Read a layout file with KLayout: top cells, every cell's "
                        "shape counts per layer, instance counts and bounding box.",
        "inputSchema": _object_schema(
            {
                "path": {"type": "string", "description": "Layout file to read."},
                "layers": {
                    "type": "object",
                    "description": "Optional layer name map {name: [layer, datatype]}. "
                                   "GDS2 stores no layer names; OASIS does.",
                },
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("path",),
        ),
    },
    {
        "name": "pyaether_layout_drc",
        "description": "Run design-rule checks with KLayout and report violations "
                        "per rule. A rule that cannot be evaluated is an error, "
                        "never a silent pass.",
        "inputSchema": _object_schema(
            {
                "path": {"type": "string", "description": "Layout file to check."},
                "rules": {
                    "type": "array",
                    "description": "Rules: [{name, check: width|space|notch|"
                                   "enclosing|area, layer, other?, value}] with "
                                   "value in microns.",
                    "items": {"type": "object"},
                },
                "layers": {
                    "type": "object",
                    "description": "Optional layer name map {name: [layer, datatype]}.",
                },
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("path", "rules"),
        ),
    },
    {
        "name": "pyaether_layout_boolean",
        "description": "Layer algebra with KLayout (merge/and/not/xor/size) into a "
                        "new layer or a new file.",
        "inputSchema": _object_schema(
            {
                "op": {"type": "string",
                       "enum": ["merge", "and", "not", "xor", "size", "grow", "shrink"]},
                "a": {"type": "string", "description": "First layer: name or "
                                                       "\"layer/datatype\"."},
                "b": {"type": "string", "description": "Second layer for and/not/xor."},
                "value": {"type": "number", "description": "Size in microns for size/grow/shrink."},
                "out_layer": {"type": "string", "description": "Layer for the result."},
                "source": {"type": "string", "description": "Input layout file."},
                "output": {"type": "string", "description": "Output layout file."},
                "layers": {"type": "object", "description": "Optional layer name map."},
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("op", "a", "out_layer"),
        ),
    },
    {
        "name": "pyaether_layout_convert",
        "description": "Convert or clip a layout with KLayout's standalone stream "
                        "tools (strm2oas, strm2gds, strm2cif, strmclip, ...). "
                        "The output suffix picks the format unless tool is given.",
        "inputSchema": _object_schema(
            {
                "source": {"type": "string", "description": "Input layout file."},
                "output": {"type": "string", "description": "Output layout file."},
                "tool": {"type": "string",
                         "description": "Tool key: oasis/gds/cif/dxf/txt/mag/lstr/clip."},
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("source", "output"),
        ),
    },
    {
        "name": "pyaether_layout_compare",
        "description": "Compare two layouts (strmcmp) or XOR them (strmxor). "
                        "Answer is data.identical / data.returncode: 0 means the "
                        "geometries match, non-zero means they differ.",
        "inputSchema": _object_schema(
            {
                "a": {"type": "string", "description": "First layout file."},
                "b": {"type": "string", "description": "Second layout file."},
                "tool": {"type": "string", "enum": ["compare", "xor"], "default": "compare"},
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("a", "b"),
        ),
    },
    {
        "name": "pyaether_layout_deck",
        "description": "Run a KLayout rule deck (.drc / .lvs) through the real "
                        "engine. Returns the deck's exit code, log and any report "
                        "database. A deck that runs but writes no report is "
                        "reported as PARTIAL, never as 'design rule clean'.",
        "inputSchema": _object_schema(
            {
                "script": {"type": "string", "description": "Rule deck (.drc or .lvs)."},
                "source": {"type": "string",
                           "description": "Layout passed to the deck as $source."},
                "top": {"type": "string", "description": "Top cell name for the deck."},
                "define": {"type": "array", "items": {"type": "string"},
                           "description": "Extra NAME=VALUE variables for -rd."},
                "timeout": {"type": "number", "minimum": 1, "maximum": 86400,
                            "default": 600},
            },
            ("script",),
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


def _format_layout(result):
    """Render a layout operation: status first, then the numbers that matter."""
    operation = result.get("operation") or "?"
    lines = ["status: %s (ok=%s)" % (result.get("status"), result.get("ok")),
             "operation: %s" % operation]
    metadata = result.get("metadata") or {}
    for key in ("engine", "target", "artifact"):
        if metadata.get(key):
            lines.append("%s: %s" % (key, metadata[key]))
    data = result.get("data") or {}
    if operation == "gen":
        lines.append("top: %s" % data.get("top"))
        lines.append("counts: %s" % json.dumps(data.get("counts"), ensure_ascii=False))
        if data.get("bbox_um"):
            lines.append("bbox_um: %s" % data["bbox_um"])
        lines.append("layers: %s" % ", ".join(data.get("layers") or []))
    elif operation == "info":
        lines.append("top_cells: %s" % ", ".join(data.get("top_cells") or []))
        for cell in data.get("cells") or []:
            lines.append("cell %s: shapes=%s instances=%s %s"
                         % (cell.get("name"), cell.get("shapes"), cell.get("instances"),
                            json.dumps(cell.get("shapes_by_layer"), ensure_ascii=False)))
    elif operation == "drc":
        lines.append("clean: %s" % data.get("clean"))
        lines.append("violations: %s" % data.get("violations"))
        for rule in data.get("rules") or []:
            text = "rule %s [%s]: %s" % (rule.get("name"), rule.get("check"),
                                         rule.get("violations"))
            if rule.get("error"):
                text += " ERROR: %s" % rule["error"][:200]
            lines.append(text)
            for marker in (rule.get("markers") or [])[:5]:
                lines.append("  at %s" % marker.get("bbox_um"))
    elif operation == "boolean":
        lines.append("op: %s" % data.get("op"))
        lines.append("polygons: %s" % data.get("polygons"))
        if data.get("bbox_um"):
            lines.append("bbox_um: %s" % data["bbox_um"])
    for error in result.get("errors") or []:
        lines.append("error: %s" % error)
    for warning in (result.get("warnings") or [])[:5]:
        lines.append("warning: %s" % warning)
    return "\n".join(lines)


def _tool_layout_gen(arguments):
    spec = arguments.get("spec")
    if not isinstance(spec, dict) or not spec:
        raise _BadArgument("argument spec must be a non-empty object")
    output = arguments.get("output")
    if output is not None and not isinstance(output, str):
        raise _BadArgument("argument output must be a string")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.generate(spec, output, timeout=timeout)
    except layout.LayoutError as exc:
        return "layout generation could not start: %s" % exc, True
    return _format_layout(result), not bool(result.get("ok"))


def _tool_layout_info(arguments):
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise _BadArgument("argument path must be a non-empty string")
    layers = arguments.get("layers")
    if layers is not None and not isinstance(layers, dict):
        raise _BadArgument("argument layers must be an object")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.info(path, layers=layers, timeout=timeout)
    except layout.LayoutError as exc:
        return "layout info could not start: %s" % exc, True
    return _format_layout(result), not bool(result.get("ok"))


def _tool_layout_drc(arguments):
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise _BadArgument("argument path must be a non-empty string")
    rules = arguments.get("rules")
    if not isinstance(rules, list) or not rules:
        raise _BadArgument("argument rules must be a non-empty array")
    layers = arguments.get("layers")
    if layers is not None and not isinstance(layers, dict):
        raise _BadArgument("argument layers must be an object")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.drc(path, rules, layers=layers, timeout=timeout)
    except layout.LayoutError as exc:
        return "layout drc could not start: %s" % exc, True
    # Note for callers: a clean run and a run with violations both execute
    # successfully; `clean` in the text says which one happened.
    return _format_layout(result), not bool(result.get("ok"))


def _tool_layout_boolean(arguments):
    op = arguments.get("op")
    if not isinstance(op, str):
        raise _BadArgument("argument op must be a string")
    a = arguments.get("a")
    if not isinstance(a, (str, list)):
        raise _BadArgument("argument a must be a layer name or [layer, datatype]")
    out_layer = arguments.get("out_layer")
    if not isinstance(out_layer, (str, list)):
        raise _BadArgument("argument out_layer must be a layer name or [layer, datatype]")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layers = arguments.get("layers")
    if layers is not None and not isinstance(layers, dict):
        raise _BadArgument("argument layers must be an object")
    layout = _load("layout")
    try:
        result = layout.boolean(op, a, b=arguments.get("b"), out_layer=out_layer,
                                output=arguments.get("output"),
                                source=arguments.get("source"),
                                value=arguments.get("value"), layers=layers,
                                timeout=timeout)
    except layout.LayoutError as exc:
        return "layout boolean could not start: %s" % exc, True
    return _format_layout(result), not bool(result.get("ok"))


def _tool_layout_convert(arguments):
    source = arguments.get("source")
    output = arguments.get("output")
    for name, value in (("source", source), ("output", output)):
        if not isinstance(value, str) or not value.strip():
            raise _BadArgument("argument %s must be a non-empty string" % name)
    tool = arguments.get("tool")
    if tool is not None and not isinstance(tool, str):
        raise _BadArgument("argument tool must be a string")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.convert(source, output, tool=tool, timeout=timeout)
    except layout.LayoutError as exc:
        return "layout convert could not start: %s" % exc, True
    return _format_layout(result), not bool(result.get("ok"))


def _tool_layout_compare(arguments):
    a = arguments.get("a")
    b = arguments.get("b")
    for name, value in (("a", a), ("b", b)):
        if not isinstance(value, str) or not value.strip():
            raise _BadArgument("argument %s must be a non-empty string" % name)
    tool = arguments.get("tool", "compare")
    if tool not in ("compare", "xor"):
        raise _BadArgument("argument tool must be 'compare' or 'xor'")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.compare(a, b, tool=tool, timeout=timeout)
    except layout.LayoutError as exc:
        return "layout compare could not start: %s" % exc, True
    # Not an error when the layouts differ: the answer is in data.identical.
    text = _format_layout(result) + "\nidentical: %s (exit code %s, 0 = identical)" % (
        (result.get("data") or {}).get("identical"),
        (result.get("data") or {}).get("returncode"))
    return text, not bool(result.get("ok"))


def _tool_layout_deck(arguments):
    script = arguments.get("script")
    if not isinstance(script, str) or not script.strip():
        raise _BadArgument("argument script must be a non-empty string")
    source = arguments.get("source")
    top = arguments.get("top")
    for name, value in (("source", source), ("top", top)):
        if value is not None and not isinstance(value, str):
            raise _BadArgument("argument %s must be a string" % name)
    define = arguments.get("define")
    if define is not None and not isinstance(define, list):
        raise _BadArgument("argument define must be an array of NAME=VALUE strings")
    timeout = _clamp_number(arguments.get("timeout", 600), "timeout", 1, 86400, False)
    layout = _load("layout")
    try:
        result = layout.deck(script, source=source, top=top, timeout=timeout,
                             extra_args=define)
    except layout.LayoutError as exc:
        return "layout deck could not start: %s" % exc, True
    return _format_layout(result), not bool(result.get("ok"))


_TOOL_HANDLERS.update(
    {
        "pyaether_status": _tool_status,
        "pyaether_api_search": _tool_api_search,
        "pyaether_api_help": _tool_api_help,
        "pyaether_exec": _tool_exec,
        "pyaether_sim_run": _tool_sim_run,
        "pyaether_layout_gen": _tool_layout_gen,
        "pyaether_layout_info": _tool_layout_info,
        "pyaether_layout_drc": _tool_layout_drc,
        "pyaether_layout_boolean": _tool_layout_boolean,
        "pyaether_layout_convert": _tool_layout_convert,
        "pyaether_layout_compare": _tool_layout_compare,
        "pyaether_layout_deck": _tool_layout_deck,
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
