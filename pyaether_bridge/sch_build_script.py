# -*- coding: utf-8 -*-
"""Runs inside the resident pyAether session: snapshot spec -> real schematic.

The spec and the report travel as files, so no user text is ever interpolated
into code (the same rule the canvas importer states for its own SKILL output).
Run by ``schematic.build()``; the three placeholders are substituted first.
"""

import json

import pyAether

SPEC_PATH = __SPEC__
REPORT_PATH = __REPORT__
LIB_DEFS = __LIBDEFS__

spec = json.load(open(SPEC_PATH))
namespace = pyAether.emyUnixNS()
problems = []
created = {"instances": 0, "nets": 0, "inst_terms": 0, "terminals": 0}


def scalar(text):
    return pyAether.emyScalarName(namespace, text)


def name(text):
    return pyAether.emyName(namespace, text)


def view_type(text):
    return pyAether.emyViewType.get(pyAether.emyReservedViewType(text))


def report(payload):
    with open(REPORT_PATH, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)


def main():
    if LIB_DEFS:
        pyAether.dbSwitchLibDefsPath(LIB_DEFS)
    source = spec["source"]
    created_library = False
    # A target library that does not exist yet is created instead of failing:
    # dbOpenLib returns None for an unknown library (verified), and dbCreateLib
    # is the API for making one. This is what lets a fresh canvas drawing land
    # in a new library without any manual setup.
    try:
        library = pyAether.dbOpenLib(source["library"], "w")
    except Exception:
        library = None
    if library is None:
        try:
            library = pyAether.dbCreateLib(source["library"])
            created_library = library is not None
        except Exception as exc:
            raise RuntimeError("library %s does not exist and could not be created: "
                               "%s: %s" % (source["library"], type(exc).__name__, exc))
        if library is None:
            raise RuntimeError("library %s could not be created" % source["library"])
    design = pyAether.emyDesign.open(scalar(source["library"]), scalar(source["cell"]),
                                     scalar(source["view"]), view_type("schematic"), "a")
    if design is None:
        raise RuntimeError("emyDesign.open returned None for %s/%s/%s"
                           % (source["library"], source["cell"], source["view"]))
    block = design.getTopBlock()
    if block is None:
        block = pyAether.emyBlock.create(design)

    visibility = pyAether.emyBlockDomainVisibility(pyAether.emcInheritFromTopBlock)
    status = pyAether.emyPlacementStatus(pyAether.emcNonePlacementStatus)

    made = {}
    for item in spec["instances"]:
        try:
            master = pyAether.emyDesign.open(
                scalar(item["library"]), scalar(item["cell"]), scalar(item["view"]),
                view_type("schematicSymbol"), "r")
        except Exception as exc:
            problems.append("master %s/%s/%s: %s: %s"
                            % (item["library"], item["cell"], item["view"],
                               type(exc).__name__, exc))
            continue
        if master is None:
            problems.append("master %s/%s/%s not found"
                            % (item["library"], item["cell"], item["view"]))
            continue
        position = item.get("position") or [0, 0]
        # emyTransform takes an emyPoint, which is integer-typed: passing floats
        # raises "Wrong number or type of arguments for new_emyTransform"
        # (measured). Coordinates in a spec may be fractional, so round them.
        point = (int(round(float(position[0]))), int(round(float(position[1]))))
        transform = pyAether.emyTransform(point, pyAether.emcR0)
        try:
            obj = pyAether.emyScalarInst.create(block, master, scalar(item["name"]),
                                                transform, None, visibility, status)
        except Exception as exc:
            problems.append("instance %s: %s: %s"
                            % (item["name"], type(exc).__name__, exc))
            continue
        if obj is None:
            problems.append("instance %s was not created" % item["name"])
            continue
        made[item["id"]] = obj
        made[item["name"]] = obj
        created["instances"] += 1

    nets = {}
    for item in spec["nets"]:
        try:
            net = pyAether.emyScalarNet.create(
                block, scalar(item["name"]),
                pyAether.emySigType(pyAether.emcSignalSigType),
                bool(item.get("global")), visibility)
        except Exception as exc:
            problems.append("net %s: %s: %s" % (item["name"], type(exc).__name__, exc))
            continue
        if net is None:
            problems.append("net %s was not created" % item["name"])
            continue
        nets[item["id"]] = net
        nets[item["name"]] = net
        created["nets"] += 1
        for term in item["terminals"]:
            inst = made.get(term["instance"])
            if inst is None:
                problems.append("net %s references unknown instance %s"
                                % (item["name"], term["instance"]))
                continue
            try:
                link = pyAether.emyInstTerm.create(net, inst, name(term["pin"]),
                                                   visibility)
            except Exception as exc:
                problems.append("net %s pin %s on %s: %s: %s"
                                % (item["name"], term["pin"], term["instance"],
                                   type(exc).__name__, exc))
                continue
            if link is None:
                problems.append("net %s could not attach %s.%s"
                                % (item["name"], term["instance"], term["pin"]))
                continue
            created["inst_terms"] += 1

    for item in spec["terminals"]:
        net = nets.get(item.get("net")) if item.get("net") else None
        if net is None:
            problems.append("terminal %s has no net" % item["name"])
            continue
        try:
            obj = pyAether.emyTerm.create(
                net, name(item["name"]),
                pyAether.emyTermType(pyAether.emcInputOutputTermType), visibility)
        except Exception as exc:
            problems.append("terminal %s: %s: %s" % (item["name"], type(exc).__name__, exc))
            continue
        if obj is None:
            problems.append("terminal %s was not created" % item["name"])
            continue
        created["terminals"] += 1

    design.save()
    design.close()
    return {"ok": True, "created": created, "problems": problems,
            "target": source, "library_created": created_library}


try:
    report(main())
except Exception as exc:
    report({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
            "created": created, "problems": problems,
            "target": spec.get("source")})
