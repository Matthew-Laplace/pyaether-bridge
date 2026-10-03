# -*- coding: utf-8 -*-
"""Runs inside the resident pyAether session: real schematic -> snapshot.

Reads one cellview and writes the ``analog-agent.schematic`` snapshot that the
canvas tools use, so a schematic drawn in Aether opens in a canvas. Executed by
``schematic.snapshot()``; the placeholders are substituted first.

Notes from the live probe that shaped this code:
  * ``getName()`` is a SWIG out-parameter call: ``obj.getName(ns, out_string)``.
    Calling it with no arguments raises a TypeError.
  * Library definitions must be registered first: ``dbSwitchLibDefsPath``.
    Without it ``openDesign`` fails with "Library <name> not found".
"""

import json
import re

import pyAether

OUT_PATH = __OUT__
TARGET = __TARGET__
LIB_DEFS = __LIBDEFS__

namespace = pyAether.emyUnixNS()


def name_of(obj):
    out = pyAether.emyString()
    try:
        obj.getName(namespace, out)
        return str(out)
    except Exception:
        return ""


def text_of(scalar_name):
    """Read a library/cell name out of an out-parameter ``emyScalarName``.

    ``str()`` on that SWIG wrapper yields a parenthesised repr such as
    ``(basic)``, which is not a usable library name (passing it back fails with
    "Invalid character '(' in input name"). Strip the wrapper.
    """
    text = str(scalar_name)
    text = re.sub(r"^[\(\[]|[\)\]]$", "", text).strip()
    return text


def report(payload):
    with open(OUT_PATH, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)


def main():
    if LIB_DEFS:
        pyAether.dbSwitchLibDefsPath(LIB_DEFS)
    library, cell, view = TARGET
    design = pyAether.openDesign(library, cell, view, "schematic", namespace, "r")
    if design is None:
        raise RuntimeError("openDesign(%s/%s/%s) returned None" % (library, cell, view))
    block = design.getTopBlock()
    if block is None:
        raise RuntimeError("the design has no top block")

    instances = []
    for inst in block.getInsts():
        lib = pyAether.emyScalarName()
        master_cell = pyAether.emyScalarName()
        try:
            inst.getLibName(lib)
        except Exception:
            pass
        try:
            inst.getCellName(master_cell)
        except Exception:
            pass
        terminals = []
        for link in inst.getInstTerms():
            net = link.getNet()
            term = link.getTerm()
            terminals.append({
                "name": name_of(term) if term else "",
                "netId": name_of(net) if net else "",
            })
        name = name_of(inst)
        instances.append({
            "id": name,
            "name": name,
            "library": text_of(lib),
            "cell": text_of(master_cell),
            "view": "symbol",
            "terminals": terminals,
        })

    nets = []
    for net in block.getNets():
        links = []
        for link in net.getInstTerms():
            inst = link.getInst()
            term = link.getTerm()
            links.append({
                "instanceId": name_of(inst) if inst else "",
                "pinName": name_of(term) if term else "",
            })
        name = name_of(net)
        nets.append({"id": name, "name": name, "isGlobal": False, "terminals": links})

    terminals = []
    for term in block.getTerms():
        net = term.getNet()
        terminals.append({
            "name": name_of(term),
            "netId": name_of(net) if net else "",
            "direction": "inputOutput",
        })

    return {
        "ok": True,
        "format": "analog-agent.schematic",
        "schemaVersion": 1,
        "source": {"library": library, "cell": cell, "view": view},
        "coordinates": {"yAxis": "up"},
        "instances": instances,
        "nets": nets,
        "terminals": terminals,
    }


try:
    report(main())
except Exception as exc:
    report({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
