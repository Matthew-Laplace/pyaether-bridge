# -*- coding: utf-8 -*-
"""Layout work through KLayout: generate GDS/OASIS, inspect it, check it.

KLayout is driven the way its authors support headless use -- ``klayout -b -r
<script>`` with the ``pya`` module KLayout ships (verified on 0.30.10: a box
written into a cell read back as ``layer 1/0 box=(0,0;1000,500)``). No Python
package is installed, so the project keeps its standard-library-only rule and
the layout engine stays a separate, user-installed tool.

Why a spec instead of "draw this in the GUI"
    Analog/mixed-signal work needs geometry that can be reproduced: test
    structures, device arrays, guard rings, pads, routing stubs -- and the
    checks that go with them. Each operation builds a KLayout script, runs it on
    a target (this machine, a container, or a server over ssh), and reads back a
    JSON report. Nothing is inferred from stdout.

Operations
    gen       layout spec (JSON) -> GDS2/OASIS
    info      any readable layout -> cells, layers, shape counts, area, bbox
    drc       rules -> width / space / notch / enclosing / area violations
    boolean   layer algebra (merge, and, not, xor, size) -> new file

Honesty rules, as in the simulator layer:
  1. ``ok`` is the execution contract; the report file is the only evidence.
  2. Violation markers are capped, so one bad rule cannot flood the caller.
  3. A check that could not run is reported as an error -- never as "clean".
"""

from __future__ import annotations

import json
import os
import shutil
import shlex
import tempfile
import time
import uuid

from . import config, transports

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILURE = "FAILURE"

# Checks implemented against pya.Region; all verified present in KLayout 0.30.10.
DRC_CHECKS = ("width", "space", "notch", "enclosing", "area")
BOOLEAN_OPS = ("merge", "and", "not", "xor", "size", "grow", "shrink")

MAX_MARKERS = 20
SCRIPT_NAME = "klayout_script.py"

# Standalone stream tools shipped with KLayout (KLayout.app/Contents/Buddy on
# macOS). They convert, clip, compare and XOR layout files without Python.
STREAM_TOOLS = {
    "oasis": "strm2oas", "gds": "strm2gds", "cif": "strm2cif", "dxf": "strm2dxf",
    "txt": "strm2txt", "mag": "strm2mag", "lstr": "strm2lstr",
    "gdstxt": "strm2gdstxt", "clip": "strmclip", "compare": "strmcmp",
    "xor": "strmxor",
}

# Suffixes KLayout dispatches to a rule-deck interpreter: `klayout -b -r x.drc`
# runs the DRC engine over the deck's own DSL, and the same for LVS. This is the
# standard way to run a foundry-style rule file, and it is what `deck()` uses.
DECK_SUFFIXES = (".drc", ".lvs", ".lydrc", ".lylvs")


class LayoutError(RuntimeError):
    """Unsupported operation, unusable spec, or a failed staging step."""


def operations():
    """Static description of what this module can do (no probing)."""
    return {
        "engine": "klayout",
        "binary_config": "PYAETHER_KLAYOUT_BIN",
        "default_binary": "klayout",
        "invocation": "klayout -b -r <script>",
        "operations": {
            "gen": "layout spec -> GDS2/OASIS (cells, boxes, polygons, paths, "
                   "texts, instance arrays)",
            "info": "read a layout: cells, layers, shape counts, instances, bbox",
            "drc": "checks: " + ", ".join(DRC_CHECKS),
            "boolean": "layer algebra: " + ", ".join(BOOLEAN_OPS),
        },
        "notes": "Requires a user-installed KLayout. No Python package is needed: "
                 "the script runs inside KLayout's own interpreter.",
    }


# --------------------------------------------------------------------------- #
# Result helpers
# --------------------------------------------------------------------------- #
def _result(status, operation, *, data=None, errors=None, warnings=None, metadata=None):
    return {
        "ok": status == STATUS_SUCCESS,
        "status": status,
        "operation": operation,
        "data": data or {},
        "errors": list(errors or []),
        "warnings": list(warnings or []),
        "metadata": metadata or {},
    }


def scalar(data, key):
    """Return one exact, finite real scalar (raises instead of guessing)."""
    if key not in data:
        raise ValueError("result has no key %r" % key)
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("key %r is not a real scalar" % key)
    import math
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("key %r is not finite" % key)
    return number


def bbox_um(data, key="bbox_um"):
    """Return a validated ``[x1, y1, x2, y2]`` bounding box in microns."""
    value = data.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("key %r is not a 4-element bbox" % key)
    numbers = [float(item) for item in value]
    import math
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError("key %r contains non-finite values" % key)
    return numbers


# --------------------------------------------------------------------------- #
# Target selection (same reasoning as the simulator layer)
# --------------------------------------------------------------------------- #
def resolve_target():
    """``(kind, reason)`` for where KLayout runs.

    KLayout is an open-source tool that needs no licence and is usually not part
    of the EDA installation, so when ``PYAETHER_KLAYOUT_TARGET`` is unset it
    prefers this machine if the binary is here; otherwise it follows the bridge
    target.
    """
    if config.KLAYOUT_TARGET:
        return config.KLAYOUT_TARGET.lower(), "PYAETHER_KLAYOUT_TARGET is set"
    import shutil
    found = shutil.which(config.KLAYOUT_BIN or "klayout")
    if found:
        return "local", "klayout found on this machine at %s" % found
    return (config.TRANSPORT or "local").lower(), \
        "no local klayout; falling back to the bridge target"


def transport_for_layout():
    """Build the transport selected by :func:`resolve_target`."""
    kind, _reason = resolve_target()
    if not config.KLAYOUT_TARGET and kind == (config.TRANSPORT or "").lower():
        return transports.build()
    if kind == "docker":
        return transports.DockerTransport(
            config.SIM_CONTAINER or config.CONTAINER, config.LAYOUT_WORKDIR,
            config.PYTHON, license_server=config.LICENSE_SERVER)
    if kind == "ssh":
        host = config.SIM_SSH_HOST or config.SSH_HOST
        if not host:
            raise LayoutError("layout target 'ssh' needs PYAETHER_SIM_SSH_HOST "
                              "(or PYAETHER_SSH_HOST)")
        return transports.SSHTransport(host, config.LAYOUT_WORKDIR, config.PYTHON,
                                       port=config.SIM_SSH_PORT or config.SSH_PORT,
                                       options=config.SIM_SSH_OPTS or config.SSH_OPTS)
    if kind == "local":
        return transports.LocalTransport(config.LAYOUT_WORKDIR, config.PYTHON)
    raise LayoutError("unknown layout target %r (expected docker / ssh / local)" % kind)


def _is_host(target):
    return isinstance(target, transports.LocalTransport)


def probe(*, transport=None, timeout=60.0):
    """Is KLayout usable on the target, and which version?"""
    target = transport or transport_for_layout()
    binary = config.KLAYOUT_BIN or "klayout"
    command = "command -v %s && (%s -v 2>&1 | head -3 || true)" % (
        shlex.quote(binary), shlex.quote(binary))
    kind, reason = resolve_target()
    try:
        result = target.run(command, timeout=timeout)
    except transports.TransportError as exc:
        return {"engine": "klayout", "target": target.label(), "target_reason": reason,
                "available": False, "detail": str(exc), "version": ""}
    available = result["rc"] == 0 and bool(result["stdout"])
    version = ""
    if available:
        for line in (result["stdout"] or "").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("/") and "KLayout" in stripped:
                version = stripped
                break
        if not version:
            version = (result["stdout"] or "").strip().splitlines()[-1].strip()
    return {"engine": "klayout", "target": target.label(), "target_reason": reason,
            "available": available, "version": version,
            "detail": "" if available else ("%s not found on target" % binary)}


# --------------------------------------------------------------------------- #
# Script deployment and execution
# --------------------------------------------------------------------------- #
_SCRIPT_TEMPLATE = None


def _script_source():
    global _SCRIPT_TEMPLATE
    if _SCRIPT_TEMPLATE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "klayout_script.py")
        with open(path, encoding="utf-8") as handle:
            _SCRIPT_TEMPLATE = handle.read()
    return _SCRIPT_TEMPLATE


def _render_script(params_path, layout_path, report_path):
    return (_script_source()
            .replace("__PARAMS__", params_path)
            .replace("__LAYOUT__", layout_path)
            .replace("__REPORT__", report_path)
            .replace("__MARKERS__", str(MAX_MARKERS))
            .replace("__CHECKS__", repr(list(DRC_CHECKS))))


def _load_spec(spec):
    """Spec may be a dict, a JSON string, or a path to a JSON file."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        text = spec.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError as exc:
                raise LayoutError("spec is not valid JSON: %s" % exc)
        try:
            with open(spec, encoding="utf-8") as handle:
                return json.load(handle)
        except OSError as exc:
            raise LayoutError("cannot read spec file %s: %s" % (spec, exc))
        except ValueError as exc:
            raise LayoutError("spec file %s is not valid JSON: %s" % (spec, exc))
    raise LayoutError("spec must be a dict, a JSON string, or a JSON file path")


def _stage_input(target, source, run_dir, copy_local=False):
    """Put a layout file where the runner can read it; returns that path.

    On the local target the original path is used as-is unless ``copy_local``
    asks for a copy: the stream tools write their output beside their input, so
    a compare/XOR run needs both inputs gathered in its own directory rather
    than left in the caller's tree.
    """
    name = os.path.basename(source)
    if _is_host(target) and not copy_local:
        return os.path.abspath(source)
    remote = os.path.join(run_dir, name)
    try:
        if _is_host(target):
            os.makedirs(run_dir, exist_ok=True)
            shutil.copy2(source, remote)
            return os.path.abspath(remote)
        with open(source, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise LayoutError("cannot read %s: %s" % (source, exc))
    target.write_file(remote, payload)
    return remote


def _run_operation(operation, *, spec=None, rules=None, source=None, output=None,
                   transport=None, workdir=None, timeout=None, run_id=None,
                   unit="um", layers=None):
    """Shared pipeline for every operation."""
    target = transport or transport_for_layout()
    timeout = float(timeout or config.LAYOUT_TIMEOUT or 600)
    kind, reason = resolve_target()
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    run_dir = os.path.join(workdir or config.LAYOUT_WORKDIR,
                           "run-%s-%s" % (operation, run_id))
    params_path = os.path.join(run_dir, "params.json")
    report_path = os.path.join(run_dir, "report.json")
    script_path = os.path.join(run_dir, SCRIPT_NAME)

    started = time.time()

    def metadata(extra=None):
        info = {
            "engine": "klayout",
            "target": target.label(),
            "target_reason": reason,
            "work_dir": run_dir,
            "timings": {"total_s": round(time.time() - started, 3)},
        }
        if extra:
            info.update(extra)
        return info

    source_path = None
    if source:
        try:
            source_path = _stage_input(target, source, run_dir)
        except LayoutError as exc:
            return _result(STATUS_FAILURE, operation, errors=[str(exc)],
                           metadata=metadata())

    # The script uses one layout path per run: for `gen`/`boolean` it is the
    # file being produced, for `info`/`drc` the file being read. A local run
    # touches the caller's path directly; a container/ssh run works inside the
    # run directory and the artifact is fetched back afterwards.
    host_output = os.path.abspath(output) if output else None
    if operation not in ("gen", "boolean"):
        layout_path = source_path or ""
    elif host_output and _is_host(target):
        layout_path = host_output
    elif host_output:
        layout_path = os.path.join(run_dir, os.path.basename(host_output))
    else:
        layout_path = os.path.join(run_dir, "out.gds")

    params = {"op": operation, "unit": unit, "_layers": layers or {}}
    if spec is not None:
        params["spec"] = spec
        spec_layers = spec.get("layers")
        if spec_layers:
            params["_layers"] = dict(layers or {}, **spec_layers)
    if rules is not None:
        params["rules"] = rules
    if operation == "boolean" and spec is not None and source_path:
        spec.setdefault("input", source_path)
        spec.setdefault("output", layout_path)

    try:
        target.run("mkdir -p %s" % shlex.quote(run_dir), timeout=60.0)
        target.write_file(script_path, _render_script(params_path, layout_path,
                                                      report_path).encode("utf-8"))
        target.write_file(params_path, json.dumps(params).encode("utf-8"))
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, operation,
                       errors=["staging failed: %s" % exc], metadata=metadata())

    binary = config.KLAYOUT_BIN or "klayout"
    command = "%s -b -r %s" % (shlex.quote(binary), shlex.quote(script_path))
    try:
        executed = target.run(command, timeout=timeout, run_dir=run_dir)
    except transports.TransportTimeout as exc:
        cleanup = target.cleanup(run_dir)
        return _result(STATUS_FAILURE, operation,
                       errors=["timed out after %.0fs: %s" % (timeout, exc),
                               "cleanup: %s" % cleanup],
                       metadata=metadata({"command": command, "timed_out": True}))
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, operation,
                       errors=["transport failure: %s" % exc],
                       metadata=metadata({"command": command}))

    local_root = tempfile.mkdtemp(prefix="pyaether-layout-")
    local_report = os.path.join(local_root, "report.json")
    report = {}
    try:
        target.fetch_file(report_path, local_report)
        with open(local_report, encoding="utf-8") as handle:
            report = json.load(handle)
    except Exception as exc:
        report = {"ok": False,
                  "error": "could not read the KLayout report: %s" % exc}

    errors = []
    warnings = []
    combined = (executed["stdout"] or "") + "\n" + (executed["stderr"] or "")
    if executed["rc"] != 0:
        errors.append("klayout exited with rc=%s" % executed["rc"])
    if not report:
        errors.append("no report was produced by the KLayout script")
    elif not report.get("ok", False):
        errors.append("klayout script failed: %s" % (report.get("error") or "unknown"))
        detail = (report.get("detail") or "").strip()
        if detail:
            errors.append(detail.splitlines()[-1][:300])

    # A rule or check that could not be evaluated must not look like "no
    # violations". Surface it at the top level and fail the run: a check that
    # did not run tells the caller nothing about the layout.
    failed_rules = []
    for item in (report.get("rules") or []):
        if isinstance(item, dict) and item.get("error"):
            failed_rules.append("%s: %s" % (item.get("name") or "rule", item["error"]))
    if failed_rules:
        errors.extend("rule could not be evaluated -- %s" % item for item in failed_rules)
    for line in combined.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("error") or "error:" in lowered:
            if "0 errors" not in lowered:
                errors.append(line.strip()[:300])
        elif "warning" in lowered and "0 warnings" not in lowered:
            warnings.append(line.strip()[:300])

    data = dict(report)
    data.pop("ok", None)

    # Bring the produced layout back to the caller's path when it was written
    # somewhere else (container / ssh target).
    fetched = None
    if host_output and not _is_host(target) and report.get("ok"):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(host_output)), exist_ok=True)
            target.fetch_file(layout_path, os.path.abspath(host_output))
            fetched = os.path.abspath(host_output)
        except Exception as exc:
            errors.append("could not fetch the layout back: %s" % exc)

    if errors:
        status = STATUS_FAILURE
    elif report.get("ok"):
        status = STATUS_SUCCESS
    else:
        status = STATUS_PARTIAL

    metadata_extra = {"command": command, "artifact": fetched or layout_path,
                      "returncode": executed["rc"]}
    if fetched:
        metadata_extra["host_artifact"] = fetched
    if source_path:
        metadata_extra["source"] = source_path
    return _result(status, operation, data=data, errors=errors, warnings=warnings,
                   metadata=metadata(metadata_extra))


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #
def generate(spec, output=None, *, transport=None, workdir=None, timeout=None,
             run_id=None, unit="um"):
    """Build a layout from a spec dict/JSON and write GDS2 or OASIS."""
    document = _load_spec(spec)
    if "cells" not in document:
        raise LayoutError("the spec needs a 'cells' object")
    return _run_operation("gen", spec=document, output=output, transport=transport,
                          workdir=workdir, timeout=timeout, run_id=run_id, unit=unit)


def info(path, *, layers=None, transport=None, workdir=None, timeout=None,
         run_id=None, unit="um"):
    """Read a layout and report its cells, layers, shape counts and extents.

    ``layers`` maps names to ``[layer, datatype]`` for files whose layer names
    are not stored (GDS2 keeps none; OASIS does).
    """
    if not os.path.isfile(path):
        raise LayoutError("layout file not found: %s" % path)
    return _run_operation("info", source=path, transport=transport, workdir=workdir,
                          timeout=timeout, run_id=run_id, unit=unit, layers=layers)


def drc(path, rules, *, layers=None, transport=None, workdir=None, timeout=None,
        run_id=None, unit="um"):
    """Run design-rule checks and report violations.

    ``rules`` is a list of dicts::

        [{"name": "m1 width", "check": "width", "layer": "m1", "value": 0.2},
         {"name": "m2 enclose m1", "check": "enclosing", "layer": "m2",
          "other": "m1", "value": 0.1}]

    Values are in microns (``unit`` selects the unit for ``gen`` specs). A rule's
    ``layer``/``other`` is either a ``[layer, datatype]`` pair or a name resolved
    through ``layers`` (or the file's own layer names). A rule that cannot be
    evaluated fails the run instead of reporting zero violations.
    """
    if not os.path.isfile(path):
        raise LayoutError("layout file not found: %s" % path)
    if isinstance(rules, str):
        text = rules.strip()
        if text.startswith("["):
            rules = json.loads(text)
        else:
            with open(rules, encoding="utf-8") as handle:
                rules = json.load(handle)
    if not isinstance(rules, list) or not rules:
        raise LayoutError("rules must be a non-empty list")
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("check"):
            raise LayoutError("every rule needs a 'check'; got %r" % (rule,))
    return _run_operation("drc", rules=rules, source=path, transport=transport,
                          layers=layers,
                          workdir=workdir, timeout=timeout, run_id=run_id, unit=unit)


def boolean(op, a, *, b=None, out_layer=None, output=None, source=None, value=None,
            transport=None, workdir=None, timeout=None, run_id=None, unit="um"):
    """Layer algebra between two layers of one layout file."""
    if op not in BOOLEAN_OPS:
        raise LayoutError("unsupported boolean op %r (expected %s)"
                          % (op, ", ".join(BOOLEAN_OPS)))
    if op in ("and", "not", "xor") and b is None:
        raise LayoutError("op %r needs layer 'b'" % op)
    if op in ("size", "grow", "shrink") and value is None:
        raise LayoutError("op %r needs 'value'" % op)
    if not out_layer:
        raise LayoutError("'out_layer' is required")
    spec = {"op": op, "a": a, "out_layer": out_layer}
    if b is not None:
        spec["b"] = b
    if value is not None:
        spec["value"] = value
    # The output path is decided in _run_operation: a remote runner must write
    # inside its own run directory and the file is fetched back afterwards.
    return _run_operation("boolean", spec=spec, source=source, output=output,
                          transport=transport, workdir=workdir, timeout=timeout,
                          run_id=run_id, unit=unit)


# --------------------------------------------------------------------------- #
# Stream tools (no Python at all) and rule decks (the DRC/LVS DSL)
# --------------------------------------------------------------------------- #
def _local_buddy_candidates():
    """Directories that could hold the stream tools on this machine."""
    candidates = []
    if config.KLAYOUT_BUDDY_DIR:
        return [config.KLAYOUT_BUDDY_DIR]
    binary = config.KLAYOUT_BIN or "klayout"
    # Resolve the real path: the usual install is a symlink into an app bundle
    # (for example ~/.local/bin/klayout -> KLayout.app/Contents/MacOS/klayout).
    real = os.path.realpath(shutil.which(binary) or binary)
    for start in (os.path.dirname(real), os.path.dirname(os.path.realpath(binary))):
        if not start:
            continue
        # <root>/MacOS/klayout         -> <root>/Buddy  and  <app>/Contents/Buddy
        # <root>/bin/klayout           -> <root>/Buddy
        candidates.append(os.path.join(start, "..", "Buddy"))
        candidates.append(os.path.join(start, "Buddy"))
        candidates.append(os.path.join(start, "..", "MacOS", "Buddy"))
    return [os.path.normpath(path) for path in candidates]


def _buddy_dir(target):
    """Directory holding KLayout's standalone stream tools, or "".

    They are not on PATH: on macOS they sit in
    ``KLayout.app/Contents/Buddy``. An explicit ``PYAETHER_KLAYOUT_BUDDY_DIR``
    wins; otherwise the directory is inferred from the klayout binary.
    """
    if config.KLAYOUT_BUDDY_DIR:
        return config.KLAYOUT_BUDDY_DIR
    if not _is_host(target):
        # On a remote target, ask it where the tools live next to its klayout.
        binary = config.KLAYOUT_BIN or "klayout"
        probe = target.run(
            "B=$(command -v %s || true); [ -n \"$B\" ] && "
            "R=$(readlink -f \"$B\" 2>/dev/null || echo \"$B\"); "
            "for D in \"$(dirname \"$R\")/Buddy\" \"$(dirname \"$R\")/../Buddy\" "
            "\"$(dirname \"$R\")/../MacOS/Buddy\"; do "
            "[ -d \"$D\" ] && { echo \"$D\"; break; }; done"
            % shlex.quote(binary), timeout=60.0)
        return (probe["stdout"] or "").strip().splitlines()[0] if probe["stdout"].strip() else ""
    for candidate in _local_buddy_candidates():
        if os.path.isdir(candidate):
            return candidate
    return ""


def stream_tools(*, transport=None):
    """Which standalone KLayout tools exist on the target."""
    target = transport or transport_for_layout()
    directory = _buddy_dir(target)
    found = {}
    if directory:
        listing = target.run("ls -1 %s 2>/dev/null" % shlex.quote(directory),
                             timeout=60.0)
        present = {name.strip() for name in (listing["stdout"] or "").splitlines()}
        for key, tool in STREAM_TOOLS.items():
            if tool in present:
                found[key] = os.path.join(directory, tool)
    else:
        for key, tool in STREAM_TOOLS.items():
            where = shutil.which(tool)
            if where:
                found[key] = where
    return found


def _run_tool(tool_path, arguments, *, transport=None, workdir=None, timeout=None,
              run_id=None, operation="convert", prefix=""):
    """Run one KLayout helper tool and report its exit code and output."""
    target = transport or transport_for_layout()
    timeout = float(timeout or config.LAYOUT_TIMEOUT or 600)
    kind, reason = resolve_target()
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    run_dir = os.path.join(workdir or config.LAYOUT_WORKDIR, "run-%s-%s"
                           % (operation, run_id))
    command = "%s%s %s" % (prefix, shlex.quote(tool_path), arguments)
    try:
        # The transport records the process group in this directory, so it has
        # to exist before the command runs.
        target.run("mkdir -p %s" % shlex.quote(run_dir), timeout=60.0)
        executed = target.run(command, timeout=timeout, run_dir=run_dir)
    except transports.TransportTimeout as exc:
        cleanup = target.cleanup(run_dir)
        return _result(STATUS_FAILURE, operation,
                       errors=["timed out after %.0fs: %s" % (timeout, exc),
                               "cleanup: %s" % cleanup],
                       metadata={"target": target.label(), "command": command,
                                 "timed_out": True})
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, operation, errors=["transport failure: %s" % exc],
                       metadata={"target": target.label(), "command": command})
    # strmcmp/strmxor use the exit code as the answer: 0 means identical.
    errors = [] if executed["rc"] == 0 else [
        "tool exited with rc=%s" % executed["rc"]]
    return _result(
        STATUS_SUCCESS if executed["rc"] == 0 else STATUS_FAILURE, operation,
        data={"returncode": executed["rc"],
              "stdout": (executed["stdout"] or "")[-4000:],
              "stderr": (executed["stderr"] or "")[-4000:]},
        errors=errors,
        metadata={"target": target.label(), "target_reason": reason,
                  "command": command, "work_dir": run_dir, "tool": tool_path})


def convert(source, output, *, tool=None, transport=None, workdir=None, timeout=None,
            run_id=None):
    """Convert or clip a layout with KLayout's standalone stream tools.

    ``tool`` selects the tool (``oasis``, ``gds``, ``cif``, ``dxf``, ``txt``,
    ``mag``, ``lstr``, ``gdstxt``, ``clip``); the default follows the output
    suffix. Use ``tools()`` to see what the target has.
    """
    if tool is None:
        suffix = os.path.splitext(output)[1].lower().lstrip(".")
        tool = {"oas": "oasis", "oasis": "oasis", "gds": "gds", "gds2": "gds",
                "cif": "cif", "dxf": "dxf", "txt": "txt",
                "mag": "mag", "lstr": "lstr"}.get(suffix)
        if tool is None:
            raise LayoutError("cannot pick a tool for %r; pass tool= explicitly "
                              "(known: %s)" % (output, ", ".join(sorted(STREAM_TOOLS))))
    if tool not in STREAM_TOOLS or tool in ("compare", "xor"):
        raise LayoutError("unknown convert tool %r (known: %s)"
                          % (tool, ", ".join(sorted(k for k in STREAM_TOOLS
                                                    if k not in ("compare", "xor")))))
    if not os.path.isfile(source):
        raise LayoutError("input file not found: %s" % source)
    target = transport or transport_for_layout()
    available = stream_tools(transport=target)
    if tool not in available:
        raise LayoutError(
            "the %s tool is not available on %s; set PYAETHER_KLAYOUT_BUDDY_DIR "
            "to the directory holding KLayout's stream tools (they are not on PATH)"
            % (STREAM_TOOLS[tool], target.label()))
    source_path = (_stage_input(target, source, os.path.join(
        workdir or config.LAYOUT_WORKDIR, "run-convert-%s" % (run_id or "tmp"))))
    output_path = os.path.abspath(output) if _is_host(target) else os.path.join(
        os.path.dirname(source_path), os.path.basename(output))
    result = _run_tool(available[tool],
                       "%s %s" % (shlex.quote(source_path), shlex.quote(output_path)),
                       transport=target, workdir=workdir, timeout=timeout, run_id=run_id,
                       operation="convert")
    if result["ok"] and not _is_host(target):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
            target.fetch_file(output_path, os.path.abspath(output))
            result["metadata"]["host_artifact"] = os.path.abspath(output)
        except Exception as exc:
            result["errors"].append("could not fetch the result back: %s" % exc)
            result["ok"] = False
            result["status"] = STATUS_FAILURE
    result["metadata"]["output"] = output_path
    return result


def compare(a, b, *, tool="compare", transport=None, workdir=None, timeout=None,
            run_id=None, silent=True):
    """Compare two layouts (``strmcmp``) or XOR them (``strmxor``).

    ``strmcmp`` uses its exit code as the answer: 0 means identical, non-zero
    means they differ. ``ok`` is therefore False for a *successful* comparison
    that found differences -- read ``data.returncode`` (0 = identical) and
    ``data.identical`` rather than ``ok`` alone.
    """
    if tool not in ("compare", "xor"):
        raise LayoutError("tool must be 'compare' or 'xor'")
    for path in (a, b):
        if not os.path.isfile(path):
            raise LayoutError("input file not found: %s" % path)
    target = transport or transport_for_layout()
    available = stream_tools(transport=target)
    if tool not in available:
        raise LayoutError(
            "the %s tool is not available on %s; set PYAETHER_KLAYOUT_BUDDY_DIR"
            % (STREAM_TOOLS[tool], target.label()))
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    stage = os.path.join(workdir or config.LAYOUT_WORKDIR, "run-%s-%s" % (tool, run_id))
    left = _stage_input(target, a, stage, copy_local=True)
    right = _stage_input(target, b, stage, copy_local=True)
    flags = " --silent" if (silent and tool == "compare") else ""
    if tool == "xor":
        output_path = os.path.join(stage, "xor.oas")
        arguments = "%s %s %s" % (shlex.quote(left), shlex.quote(right),
                                  shlex.quote(output_path))
    else:
        arguments = "%s %s%s" % (shlex.quote(left), shlex.quote(right), flags)
    result = _run_tool(available[tool], arguments, transport=target, workdir=workdir,
                       timeout=timeout, run_id=run_id, operation=tool)
    code = (result["data"] or {}).get("returncode")
    result["data"]["identical"] = (code == 0)
    if tool == "compare":
        # A non-zero exit is the normal "they differ" answer, not a crash.
        result["ok"] = True
        result["status"] = STATUS_SUCCESS
        result["errors"] = []
    if tool == "xor" and _is_host(target):
        result["metadata"]["output"] = output_path
    return result


def deck(script, *, source=None, top=None, timeout=None, transport=None,
         workdir=None, run_id=None, extra_args=None):
    """Run a KLayout rule deck (``.drc`` / ``.lvs``) through the real engine.

    ``klayout -b -r <deck>`` dispatches on the file suffix to KLayout's DRC or
    LVS interpreter, which is how a foundry-style rule file is normally run.
    That is a different mechanism from :func:`drc`, which performs individual
    geometric checks from a rule list.

    ``source`` and ``top`` are passed to the deck as the ``source``/``top``
    variables, so a deck written as ``source("in.gds")`` or with
    ``-rd source=...`` style overrides can be pointed at a file here. The deck's
    own log output is returned; a non-zero exit is a failure.
    """
    if not os.path.isfile(script):
        raise LayoutError("rule deck not found: %s" % script)
    suffix = os.path.splitext(script)[1].lower()
    if suffix not in DECK_SUFFIXES:
        raise LayoutError("expected a KLayout rule deck (%s), got %r"
                          % (", ".join(DECK_SUFFIXES), script))
    target = transport or transport_for_layout()
    timeout = float(timeout or config.LAYOUT_TIMEOUT or 600)
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    run_dir = os.path.join(workdir or config.LAYOUT_WORKDIR, "run-deck-%s" % run_id)
    staged = _stage_input(target, script, run_dir)
    staged_source = _stage_input(target, source, run_dir) if source else None

    # The deck writes its report relative to the working directory, so run it
    # from the run directory and look for the report there afterwards.
    binary = config.KLAYOUT_BIN or "klayout"
    arguments = ["-b"]
    if staged_source:
        arguments.extend(["-rd", "source=%s" % staged_source])
    if top:
        arguments.extend(["-rd", "top=%s" % top])
    for item in (extra_args or []):
        arguments.extend(["-rd", str(item)])
    arguments.extend(["-r", staged])
    rendered = " ".join(shlex.quote(str(item)) for item in arguments)
    if config.KLAYOUT_BIN or "/" in binary:
        invoked = binary
    else:
        invoked = binary  # resolved by the login shell's PATH on the target
    # Rule decks write their report with a relative name, so the engine has to
    # run *inside* the run directory; otherwise `report("drc.lyrdb")` lands in
    # the target's default working directory and is lost.
    # Rule decks write their report under a relative name, so the engine runs
    # *inside* the run directory; otherwise `report("drc.lyrdb")` lands in the
    # target's default working directory and is lost.
    result = _run_tool(invoked, rendered, prefix="cd %s && " % shlex.quote(run_dir),
                       transport=target, workdir=workdir, timeout=timeout,
                       run_id=run_id, operation="deck")
    result["metadata"]["deck"] = staged
    result["metadata"]["deck_kind"] = "lvs" if "lvs" in suffix else "drc"
    if staged_source:
        result["metadata"]["source"] = staged_source
    # A DRC/LVS deck leaves a report database next to where it ran; its presence
    # is the evidence that the engine really executed the rules.
    run_dir = result["metadata"]["work_dir"]
    try:
        listing = target.run("find %s -maxdepth 1 -type f 2>/dev/null | head -40"
                             % shlex.quote(run_dir), timeout=60.0)
        produced = [line.strip() for line in (listing["stdout"] or "").splitlines()
                    if line.strip()]
    except transports.TransportError:
        produced = []
    result["metadata"]["artifacts"] = produced
    result["data"]["report_artifacts"] = [path for path in produced
                                          if path.endswith((".lyrdb", ".l2n", ".l2ndb"))]
    if result["ok"] and not result["data"]["report_artifacts"]:
        # The engine runs the deck (the DSL calls are traced in its log), but on
        # KLayout 0.30.10 for macOS the report database was not written in batch
        # mode even for a deck that calls report() and has output rules. rc=0
        # therefore only means "the deck executed", never "the layout is clean".
        result["status"] = STATUS_PARTIAL
        result["ok"] = False
        result["errors"].append(
            "the deck executed but no report database (*.lyrdb / *.l2n) was "
            "written, so no rule verdict can be read from it -- treat this as "
            "'deck ran', not as 'design rule clean'. Check the log above and the "
            "deck's own report()/netlist() call")
    return result
