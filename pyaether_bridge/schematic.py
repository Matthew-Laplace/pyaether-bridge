# -*- coding: utf-8 -*-
"""Schematic round trip: draw in a canvas, generate the schematic in PyAether.

Forward tools (Aether -> canvas) already exist, so this module speaks the same
interchange format they use -- the ``analog-agent.schematic`` snapshot::

    {"format": "analog-agent.schematic", "schemaVersion": 1,
     "source": {"library", "cell", "view"},
     "instances": [{"id", "name", "library", "cell", "view", "position",
                    "terminals": [{"name", "netId"}]}],
     "nets":      [{"id", "name", "isGlobal",
                    "terminals": [{"instanceId", "pinName"}]}],
     "terminals": [{"name", "netId", "direction"}]}

An Analog Canvas document is accepted as input too: it carries the same facts
nested differently (``reference``, ``placement.position``, ``netlist.terminals``
for the formal interface), so one normalizer covers both.

Operations: ``snapshot`` (live schematic -> snapshot), ``build`` (snapshot or
canvas document -> real schematic), ``netlist`` (connectivity -> SPICE text,
no session needed), ``roundtrip`` (build, read back, compare connectivity).

Honesty rules, as in the other modules:
  1. A build reports what it created and the design it opened; nothing is
     claimed that was not read back.
  2. Input whose fields cannot be resolved is refused with the exact field
     names that were missing, rather than producing a half-empty sheet.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
import uuid

from . import config

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILURE = "FAILURE"

SNAPSHOT_FORMAT = "analog-agent.schematic"
SNAPSHOT_VERSION = 1
DEFAULT_VIEW = "schematic"


class SchematicError(RuntimeError):
    """Unusable input, missing target support, or a failed build."""


def operations():
    """Static description of the module (no probing)."""
    return {
        "engine": "pyAether (Empyrean Aether)",
        "operations": {
            "snapshot": "live schematic -> analog-agent.schematic snapshot",
            "build": "snapshot or canvas document -> schematic in Aether",
            "netlist": "connectivity -> SPICE netlist text (offline)",
            "roundtrip": "build, read back and compare the connectivity",
        },
        "notes": "snapshot/build/roundtrip need a live pyAether session; "
                 "netlist works offline from a spec.",
    }


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


def _spread_positions(document, spacing=200.0, columns=6):
    """Give instances a readable layout when the spec carries no coordinates.

    A snapshot read out of Aether has real positions; a canvas document has its
    own. Only when every position is the default (0, 0) -- which happens for a
    hand-written or minimal spec -- are they laid out on a grid, so the generated
    sheet is not a pile of overlapping symbols.
    """
    instances = document.get("instances") or []
    if not instances or any(inst["position"] != [0.0, 0.0] for inst in instances):
        return False
    for index, inst in enumerate(instances):
        inst["position"] = [100.0 + (index % columns) * spacing,
                            100.0 + (index // columns) * spacing]
    return True


def _load_spec(spec):
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        text = spec.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError as exc:
                raise SchematicError("spec is not valid JSON: %s" % exc)
        try:
            with open(spec, encoding="utf-8") as handle:
                return json.load(handle)
        except OSError as exc:
            raise SchematicError("cannot read spec file %s: %s" % (spec, exc))
        except ValueError as exc:
            raise SchematicError("spec file %s is not valid JSON: %s" % (spec, exc))
    raise SchematicError("spec must be a dict, a JSON string, or a JSON file path")


def _first(mapping, *names):
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) not in (None, ""):
            return mapping[name]
    return None


def _as_point(value, default=(0.0, 0.0)):
    """Accept ``[x, y]`` or ``{"x": .., "y": ..}`` (a canvas uses the latter)."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [float(value[0]), float(value[1])]
    if isinstance(value, dict):
        x, y = _first(value, "x", "X"), _first(value, "y", "Y")
        if x is not None and y is not None:
            return [float(x), float(y)]
    return list(default)


def normalize(spec, *, library=None, cell=None, view=None):
    """Turn a snapshot or a canvas document into one internal spec."""
    spec = _load_spec(spec)
    document = spec.get("document") if isinstance(spec.get("document"), dict) else spec
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    if not source and isinstance(document.get("sourceBinding"), dict):
        # A canvas document records where it came from here.
        source = document["sourceBinding"]
    target = {
        "library": library or _first(source, "library", "lib")
                    or _first(document, "library"),
        "cell": cell or _first(source, "cell") or _first(document, "cell"),
        "view": view or _first(source, "view") or _first(document, "view") or DEFAULT_VIEW,
    }
    if not target["library"] or not target["cell"]:
        raise SchematicError(
            "the target cell is unknown: pass library/cell explicitly, or set "
            "source.library and source.cell in the spec")

    problems, instances = [], []
    for index, raw in enumerate(document.get("instances") or []):
        if not isinstance(raw, dict):
            problems.append("instances[%d] is not an object" % index)
            continue
        reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
        lib = _first(reference, "library", "lib") or _first(raw, "library", "lib")
        master = _first(reference, "cell") or _first(raw, "cell")
        master_view = _first(reference, "view") or _first(raw, "view") or "symbol"
        if (not lib or not master) and isinstance(reference.get("source"), dict):
            lib = lib or _first(reference["source"], "library", "lib")
            master = master or _first(reference["source"], "cell")
        if not lib or not master:
            problems.append("instances[%d] (id=%r) has no master library/cell"
                            % (index, raw.get("id")))
            continue
        placement = raw.get("placement") if isinstance(raw.get("placement"), dict) else {}
        parameters = {}
        raw_params = raw.get("parameters")
        if isinstance(raw_params, dict):
            parameters = dict(raw_params)
        elif isinstance(raw_params, list):
            for item in raw_params:
                if isinstance(item, dict) and _first(item, "name"):
                    parameters[item["name"]] = item.get("value")
        instances.append({
            "id": str(raw.get("id") or raw.get("name") or "inst%d" % index),
            "name": str(raw.get("name") or raw.get("id") or "I%d" % index),
            "library": str(lib),
            "cell": str(master),
            "view": str(master_view),
            "position": _as_point(placement.get("position")
                                  if placement else raw.get("position")),
            "rotation": int(raw.get("rotation") or placement.get("rotation") or 0),
            "mirror": bool(raw.get("mirror") or placement.get("mirror") or False),
            "parameters": parameters,
        })

    names = {}
    for item in document.get("connectivityEvidence") or []:
        if not isinstance(item, dict):
            continue
        net_id = _first(item, "netId", "sourceNetId")
        claim = _first(item, "name", "sourceName")
        if net_id and claim and net_id not in names:
            names[net_id] = str(claim)

    nets = []
    for index, raw in enumerate(document.get("nets") or []):
        if not isinstance(raw, dict):
            problems.append("nets[%d] is not an object" % index)
            continue
        net_id = str(raw.get("id") or "net%d" % index)
        terminals = []
        for term in raw.get("terminals") or []:
            if not isinstance(term, dict):
                continue
            inst = _first(term, "instanceId", "instance", "inst")
            pin = _first(term, "pinName", "pin", "terminal")
            if inst and pin:
                terminals.append({"instance": str(inst), "pin": str(pin)})
        nets.append({
            "id": net_id,
            "name": str(raw.get("name") or names.get(net_id) or net_id),
            "global": bool(raw.get("isGlobal") or raw.get("global") or False),
            "terminals": terminals,
        })

    terminals = []
    interface = document.get("netlist") if isinstance(document.get("netlist"), dict) else None
    for index, raw in enumerate((interface or {}).get("terminals") or []):
        if not isinstance(raw, dict):
            continue
        terminal_name = _first(raw, "name", "id")
        if not terminal_name:
            problems.append("netlist.terminals[%d] has no name" % index)
            continue
        terminals.append({"name": str(terminal_name),
                          "net": str(_first(raw, "netId", "net") or ""),
                          "direction": str(_first(raw, "direction", "type")
                                           or "inputOutput")})
    for raw in document.get("terminals") or []:
        if isinstance(raw, dict) and _first(raw, "name"):
            terminals.append({"name": str(raw["name"]),
                              "net": str(_first(raw, "netId", "net") or ""),
                              "direction": str(_first(raw, "direction", "type")
                                               or "inputOutput")})

    normalized = {
        "format": SNAPSHOT_FORMAT,
        "schemaVersion": SNAPSHOT_VERSION,
        "source": target,
        "instances": instances,
        "nets": nets,
        "terminals": terminals,
    }
    if problems:
        normalized["problems"] = problems
    return normalized


_CACHE = {}


def _script(name):
    if name not in _CACHE:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, encoding="utf-8") as handle:
            _CACHE[name] = handle.read()
    return _CACHE[name]


def _lib_defs():
    """Where the target's library definitions live (for dbSwitchLibDefsPath).

    Without this call ``openDesign`` fails with "Library <name> not found"; the
    live probe showed the vendored libraries need registering first.

    The argument is a *directory*: ``dbSwitchLibDefsPath`` looks for ``lib.defs``
    inside it. Passing a file path fails with NotADirectoryError (measured), so a
    file is accepted here for convenience and reduced to its directory.
    """
    explicit = config.SCHEMATIC_LIBDEFS
    if explicit:
        # A caller may point at the file itself; the API wants its directory.
        if explicit.endswith((".defs", ".lib")) or os.path.isfile(explicit):
            return os.path.dirname(explicit) or "."
        return explicit
    root = config.SCHEMATIC_AETHER_ROOT
    return os.path.join(root, "lib") if root else ""


def _workdir(kind, run_id=None):
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    return os.path.join(config.SCHEMATIC_WORKDIR, "%s-%s" % (kind, run_id))


def _fetch_report(target, remote_path):
    local_dir = tempfile.mkdtemp(prefix="pyaether-sch-")
    local = os.path.join(local_dir, "report.json")
    target.fetch_file(remote_path, local)
    with open(local, encoding="utf-8") as handle:
        return json.load(handle)


def build(spec, *, library=None, cell=None, view=None, timeout=600.0, run_id=None):
    """Create a schematic in Aether from a snapshot or a canvas document."""
    from . import runtime, transports

    document = normalize(spec, library=library, cell=cell, view=view)
    if not document["instances"]:
        raise SchematicError("the spec has no instances to place")
    laid_out = _spread_positions(document)
    target = transports.build()
    run_dir = _workdir("sch-build", run_id)
    spec_path = os.path.join(run_dir, "spec.json")
    report_path = os.path.join(run_dir, "report.json")
    started = time.time()
    try:
        target.run("mkdir -p %s" % shlex.quote(run_dir), timeout=60.0)
        target.write_file(spec_path, json.dumps(document).encode("utf-8"))
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, "build",
                       errors=["could not stage the spec: %s" % exc],
                       metadata={"target": target.label(), "work_dir": run_dir})

    code = (_script("sch_build_script.py")
            .replace("__SPEC__", repr(spec_path))
            .replace("__REPORT__", repr(report_path))
            .replace("__LIBDEFS__", repr(_lib_defs())))
    result = runtime.exec_code(code, timeout=timeout)
    if not result.get("ok"):
        return _result(STATUS_FAILURE, "build",
                       errors=["session failed: %s" % (result.get("error") or "unknown")],
                       metadata={"target": target.label(), "work_dir": run_dir})
    try:
        report = _fetch_report(target, report_path)
    except Exception as exc:
        return _result(STATUS_FAILURE, "build",
                       errors=["could not read the build report: %s" % exc],
                       metadata={"target": target.label(), "work_dir": run_dir})
    created = report.get("created") or {}
    problems = report.get("problems") or []
    if not report.get("ok"):
        return _result(STATUS_FAILURE, "build",
                       errors=[report.get("error") or "build failed"] + problems,
                       metadata={"target": target.label(), "work_dir": run_dir})
    return _result(STATUS_SUCCESS if not problems else STATUS_PARTIAL, "build",
                   data={"created": created, "problems": problems,
                         "design": document["source"]},
                   errors=problems,
                   metadata={"target": target.label(), "work_dir": run_dir,
                             "spec": spec_path,
                             "auto_placed": laid_out,
                             "normalized_problems": document.get("problems") or [],
                             "timings": {"total_s": round(time.time() - started, 3)}})


def snapshot(library, cell, view=DEFAULT_VIEW, *, timeout=300.0, run_id=None):
    """Read a live schematic into an ``analog-agent.schematic`` snapshot."""
    from . import runtime, transports

    if not library or not cell:
        raise SchematicError("snapshot needs a library and a cell")
    target = transports.build()
    run_dir = _workdir("sch-snapshot", run_id)
    out_path = os.path.join(run_dir, "snapshot.json")
    started = time.time()
    try:
        target.run("mkdir -p %s" % shlex.quote(run_dir), timeout=60.0)
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, "snapshot", errors=[str(exc)],
                       metadata={"target": target.label(), "work_dir": run_dir})
    code = (_script("sch_snapshot_script.py")
            .replace("__OUT__", repr(out_path))
            .replace("__TARGET__", repr([library, cell, view]))
            .replace("__LIBDEFS__", repr(_lib_defs())))
    result = runtime.exec_code(code, timeout=timeout)
    if not result.get("ok"):
        return _result(STATUS_FAILURE, "snapshot",
                       errors=["session failed: %s" % (result.get("error") or "unknown")],
                       metadata={"target": target.label(), "work_dir": run_dir})
    try:
        report = _fetch_report(target, out_path)
    except Exception as exc:
        return _result(STATUS_FAILURE, "snapshot",
                       errors=["could not read the snapshot: %s" % exc],
                       metadata={"target": target.label(), "work_dir": run_dir})
    if not report.get("ok"):
        return _result(STATUS_FAILURE, "snapshot",
                       errors=[report.get("error") or "snapshot failed"],
                       metadata={"target": target.label(), "work_dir": run_dir})
    counts = {"instances": len(report.get("instances") or []),
              "nets": len(report.get("nets") or []),
              "terminals": len(report.get("terminals") or [])}
    return _result(STATUS_SUCCESS, "snapshot",
                   data={"snapshot": report, "counts": counts},
                   metadata={"target": target.label(), "work_dir": run_dir,
                             "timings": {"total_s": round(time.time() - started, 3)}})


def netlist(spec, *, subckt=None, library=None, cell=None, view=None):
    """Emit SPICE text from a snapshot's connectivity (no session needed).

    Derived from the same connectivity the build uses, so it can be produced and
    diffed without a live session. It is a *structural* netlist: device models
    come from the drawing, not from a PDK.
    """
    document = normalize(spec, library=library, cell=cell, view=view)
    if not document["instances"]:
        raise SchematicError("the spec has no instances")
    block_name = subckt or document["source"]["cell"]
    by_id = {}
    for inst in document["instances"]:
        by_id[inst["id"]] = inst
        by_id[inst["name"]] = inst

    pinned = {term["net"] for term in document["terminals"] if term.get("net")}
    header = [term["name"] for term in document["terminals"] if term.get("net")]
    header += sorted(net["name"] for net in document["nets"]
                     if net["name"] not in pinned)

    # Instance -> {pin: net}. A positional SPICE instance line needs the master's
    # terminal order; a snapshot read back from Aether carries it (its instance
    # terminals come from the master), a hand-written canvas document does not.
    wiring = {}
    for net in document["nets"]:
        for term in net["terminals"]:
            inst = by_id.get(term["instance"])
            if inst is None:
                continue
            wiring.setdefault(inst["name"], {})[term["pin"]] = net["name"]
    ordered = {}
    for inst in document["instances"]:
        names = [t.get("name") for t in (inst.get("terminals") or []) if t.get("name")]
        if names and set(names) >= set(wiring.get(inst["name"], {})):
            ordered[inst["name"]] = names

    lines = ["* Generated by pyaether-bridge from %s/%s/%s"
             % (document["source"]["library"], document["source"]["cell"],
                document["source"]["view"])]
    positional = len(ordered) == len(wiring)
    lines.append("* Form: %s" % ("positional SPICE instances" if positional
                                 else "connectivity edge list"))
    lines.append(".subckt %s %s" % (block_name, " ".join(header)))
    for inst in document["instances"]:
        pins = wiring.get(inst["name"])
        if not pins:
            continue
        master = "%s/%s" % (inst["library"], inst["cell"])
        if inst["name"] in ordered:
            nodes = " ".join(pins.get(pin, "NC") for pin in ordered[inst["name"]])
            lines.append("X%s %s %s" % (inst["name"], nodes, master))
        else:
            # Without a known pin order a positional deck would silently shuffle
            # nodes, so each connection is stated explicitly instead.
            for pin, net in sorted(pins.items()):
                lines.append("* %s.%s = %s" % (inst["name"], pin, net))
            lines.append("X%s %s %s"
                         % (inst["name"], " ".join(sorted(pins.values())), master))
    lines.append(".ends %s" % block_name)
    return "\n".join(lines) + "\n"


def write_netlist(spec, path, **kwargs):
    """Write :func:`netlist` output to ``path`` and return the path."""
    text = netlist(spec, **kwargs)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def connectivity(spec):
    """``(connections, pins)`` for comparison: ``(net, instance, pin)`` triples."""
    document = spec if spec.get("format") == SNAPSHOT_FORMAT else normalize(spec)
    triples = set()
    for net in document["nets"]:
        for term in net["terminals"]:
            # A snapshot read from Aether uses instanceId/pinName; the internal
            # spec uses instance/pin. Accept both so a read-back can be compared
            # with the input directly.
            instance = _first(term, "instance", "instanceId")
            pin = _first(term, "pin", "pinName")
            if instance and pin:
                triples.add((net["name"], str(instance), str(pin)))
    return triples, {term["name"] for term in document["terminals"]}


def roundtrip(spec, *, timeout=900.0, run_id=None):
    """Build the schematic, read it back, and compare the connectivity.

    This is the check that the drawing survived the trip: instance count, the
    ``(net, instance, pin)`` connections and the top-level pins are compared
    between the input and what the target reports afterwards.
    """
    document = normalize(spec)
    built = build(document, timeout=timeout, run_id=run_id)
    if not built["ok"]:
        return _result(built["status"], "roundtrip",
                       data={"build": built.get("data") or {}},
                       errors=["build failed before the read-back"]
                              + (built.get("errors") or []),
                       metadata=built.get("metadata") or {})
    source = document["source"]
    read = snapshot(source["library"], source["cell"], source["view"], timeout=timeout,
                    run_id=(run_id + "-read" if run_id else None))
    if not read["ok"]:
        return _result(STATUS_PARTIAL, "roundtrip",
                       data={"build": built.get("data") or {},
                             "read_back": read.get("data") or {}},
                       errors=["the schematic was built but could not be read back"]
                              + (read.get("errors") or []),
                       metadata=built.get("metadata") or {})
    expected_conn, expected_pins = connectivity(document)
    actual_conn, actual_pins = connectivity(read["data"]["snapshot"])
    missing = sorted(expected_conn - actual_conn)
    unexpected = sorted(actual_conn - expected_conn)
    counts = read["data"]["counts"]
    ok = (not missing and not unexpected
          and counts["instances"] == len(document["instances"])
          and expected_pins <= actual_pins)
    return _result(STATUS_SUCCESS if ok else STATUS_PARTIAL, "roundtrip",
                   data={"created": built["data"]["created"],
                         "read_back": counts,
                         "expected": {"instances": len(document["instances"]),
                                      "connections": len(expected_conn),
                                      "pins": sorted(expected_pins)},
                         "missing_connections": missing[:40],
                         "unexpected_connections": unexpected[:40],
                         "missing_connections_total": len(missing),
                         "unexpected_connections_total": len(unexpected)},
                   errors=[] if ok else ["the read-back does not match the input"],
                   metadata=built.get("metadata") or {})
