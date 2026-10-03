#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Target-side KLayout script, deployed and run by ``layout.py``.

Run as::

    klayout -b -r klayout_script.py

The three paths below are substituted by the host before deployment. The script
reads a JSON parameter file (operation + spec/rules), does the work with the
``pya`` module that ships inside KLayout, and writes a JSON report for the host
to fetch. Nothing is parsed from stdout, so log noise from KLayout cannot be
mistaken for a result.
"""

import json
import sys
import traceback

import pya

PARAMS_PATH = r"__PARAMS__"
LAYOUT_PATH = r"__LAYOUT__"
REPORT_PATH = r"__REPORT__"
MAX_MARKERS = __MARKERS__
SUPPORTED_CHECKS = __CHECKS__

PARAMS = json.load(open(PARAMS_PATH))


def report(payload):
    with open(REPORT_PATH, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)


def fail(message, detail=""):
    report({"ok": False, "error": str(message), "detail": str(detail)[:4000]})
    sys.exit(1)


def resolve_layer(ly, entry):
    """Layer reference -> LayerIndex.

    Accepts an explicit ``[layer, datatype]`` pair or a name: first the spec's
    own ``layers`` map, then the layer names already present in the file.
    """
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return ly.layer(int(entry[0]), int(entry[1]))
    if isinstance(entry, str):
        named = PARAMS.get("_layers") or {}
        if entry in named:
            ref = named[entry]
            return ly.layer(int(ref[0]), int(ref[1]))
        for info in ly.layer_infos():
            if info.name == entry:
                return ly.layer(info.layer, info.datatype)
        raise ValueError("unknown layer %r (not in the layers map and not in the file)"
                         % entry)
    raise ValueError("layer reference must be [layer, datatype] or a name, got %r"
                     % (entry,))


def unit_scale(ly):
    """Multiplier converting the spec's unit into database units."""
    if PARAMS.get("unit", "um") in ("um", "micron", "microns"):
        return 1.0 / ly.dbu
    return 1.0


def to_db(value, scale):
    return int(round(float(value) * scale))


def build_shapes(ly, cell, cell_spec, scale):
    counts = {"box": 0, "polygon": 0, "path": 0, "text": 0}
    for shape in cell_spec.get("shapes") or []:
        shapes = cell.shapes(resolve_layer(ly, shape.get("layer")))
        if "box" in shape:
            x1, y1, x2, y2 = shape["box"]
            shapes.insert(pya.Box(to_db(min(x1, x2), scale), to_db(min(y1, y2), scale),
                                  to_db(max(x1, x2), scale), to_db(max(y1, y2), scale)))
            counts["box"] += 1
        elif "polygon" in shape:
            points = [pya.Point(to_db(x, scale), to_db(y, scale))
                      for x, y in shape["polygon"]]
            shapes.insert(pya.Polygon(points))
            counts["polygon"] += 1
        elif "path" in shape:
            points = [pya.Point(to_db(x, scale), to_db(y, scale))
                      for x, y in shape["path"]]
            path = pya.Path(points, to_db(shape.get("width", 1.0), scale))
            if shape.get("begin_extension") is not None:
                path.begin_ext = to_db(shape["begin_extension"], scale)
            if shape.get("end_extension") is not None:
                path.end_ext = to_db(shape["end_extension"], scale)
            shapes.insert(path)
            counts["path"] += 1
        elif "text" in shape:
            at = shape.get("at") or [0, 0]
            shapes.insert(pya.Text(shape["text"],
                                   pya.Trans(pya.Point(to_db(at[0], scale),
                                                       to_db(at[1], scale)))))
            counts["text"] += 1
        else:
            raise ValueError("shape %r has none of box/polygon/path/text" % (shape,))
    return counts


def do_gen():
    spec = PARAMS["spec"]
    ly = pya.Layout()
    ly.dbu = float(spec.get("dbu", 0.001))
    scale = unit_scale(ly)

    # Attach the spec's layer names to the layout. OASIS keeps them; GDS2 has no
    # layer-name table, which is why the CLI/MCP also accept a --layers map when
    # reading a file back.
    for name, ref in (spec.get("layers") or {}).items():
        ly.set_info(ly.layer(int(ref[0]), int(ref[1])),
                    pya.LayerInfo(int(ref[0]), int(ref[1]), name))

    cells = {}
    for name in (spec.get("cells") or {}):
        cells[name] = ly.create_cell(name)
    counts = {"box": 0, "polygon": 0, "path": 0, "text": 0, "instance": 0}
    for name, cell_spec in (spec.get("cells") or {}).items():
        cell = cells[name]
        for key, value in build_shapes(ly, cell, cell_spec, scale).items():
            counts[key] += value
        for inst in cell_spec.get("instances") or []:
            target_name = inst["cell"]
            if target_name not in cells:
                raise ValueError("instance references undeclared cell %r" % target_name)
            at = inst.get("at") or [0, 0]
            trans = pya.Trans(pya.Point(to_db(at[0], scale), to_db(at[1], scale)))
            if inst.get("rotation"):
                trans = trans * pya.Trans(int(inst["rotation"]))
            if inst.get("mirror"):
                trans = trans * pya.Trans(pya.Trans.M0)
            target = cells[target_name]
            if inst.get("columns") or inst.get("rows"):
                cell.insert(pya.CellInstArray(
                    target.cell_index(), trans,
                    pya.Vector(to_db(inst.get("dx", 0.0), scale),
                               to_db(inst.get("dy", 0.0), scale)),
                    pya.Vector(to_db(inst.get("dx_row", 0.0), scale),
                               to_db(inst.get("dy_row", 0.0), scale)),
                    int(inst.get("columns", 1)), int(inst.get("rows", 1))))
            else:
                cell.insert(pya.CellInstArray(target.cell_index(), trans))
            counts["instance"] += 1

    top_name = spec.get("top")
    if top_name and top_name in cells:
        ly.top_cell().name = top_name
    elif not top_name:
        instanced = set()
        for cell in cells.values():
            for item in cell.each_inst():
                instanced.add(item.cell.name)
        leaves = [name for name in cells if name not in instanced]
        top_name = (leaves or list(cells) or [None])[0]
    ly.write(LAYOUT_PATH)
    top = cells.get(top_name)
    bbox = top.bbox() if top is not None else None
    report({
        "ok": True,
        "operation": "gen",
        "path": LAYOUT_PATH,
        "top": top_name,
        "dbu": ly.dbu,
        "cells": sorted(cells),
        "layers": sorted("%d/%d" % (info.layer, info.datatype)
                         for info in ly.layer_infos()),
        "counts": counts,
        "bbox_um": None if bbox is None or bbox.empty() else
                   [bbox.left * ly.dbu, bbox.bottom * ly.dbu,
                    bbox.right * ly.dbu, bbox.top * ly.dbu],
    })


def do_info():
    ly = pya.Layout()
    ly.read(LAYOUT_PATH)
    cells = []
    for cell in ly.each_cell():
        per_layer = {}
        for index in ly.layer_indices():
            count = sum(1 for _shape in cell.shapes(index).each())
            if count:
                info = ly.get_info(index)
                per_layer["%d/%d" % (info.layer, info.datatype)] = count
        bbox = cell.bbox()
        cells.append({
            "name": cell.name,
            "shapes": sum(per_layer.values()),
            "shapes_by_layer": per_layer,
            "instances": sum(1 for _item in cell.each_inst()),
            "bbox_um": None if bbox.empty() else
                       [bbox.left * ly.dbu, bbox.bottom * ly.dbu,
                        bbox.right * ly.dbu, bbox.top * ly.dbu],
        })
    report({
        "ok": True,
        "operation": "info",
        "path": LAYOUT_PATH,
        "dbu": ly.dbu,
        "top_cells": [cell.name for cell in ly.top_cells()],
        "cells": cells,
        "layers": [{"layer": info.layer, "datatype": info.datatype,
                    "name": info.name or ""} for info in ly.layer_infos()],
    })


def do_drc():
    ly = pya.Layout()
    ly.read(LAYOUT_PATH)
    scale = unit_scale(ly)
    regions = {}

    def region_for(name):
        if name not in regions:
            index = resolve_layer(ly, name)
            region = pya.Region()
            for cell in ly.top_cells():
                region += pya.Region(cell.begin_shapes_rec(index))
            regions[name] = region.merged()
        return regions[name]

    def markers_of(items):
        markers = []
        count = 0
        for item in items:
            count += 1
            if len(markers) < MAX_MARKERS:
                box = item.bbox()
                markers.append({"bbox_um": [box.left * ly.dbu, box.bottom * ly.dbu,
                                            box.right * ly.dbu, box.top * ly.dbu]})
        return count, markers

    def edge_pairs(region, kind, tolerance, other=None):
        """Run an edge-pair check and drop touching edges.

        KLayout's raw ``*_check`` methods also report pairs of edges that merely
        touch (distance 0). Those are not spacing or width violations -- they are
        the junctions of a polygon boundary -- and they show up as soon as
        shapes are merged. ``NeverIncludeZeroDistance`` removes the collinear
        cases; non-collinear touching pairs survive it, so they are filtered
        here. Both counts are reported, so a caller can see what was dropped.
        """
        mode = pya.ZeroDistanceMode.NeverIncludeZeroDistance
        if kind == "enclosing":
            pairs = region.enclosing_check(other, tolerance, zero_distance_mode=mode)
        elif kind == "width":
            pairs = region.width_check(tolerance, zero_distance_mode=mode)
        elif kind == "space":
            pairs = region.space_check(tolerance, zero_distance_mode=mode)
        else:
            pairs = region.notch_check(tolerance, zero_distance_mode=mode)
        kept = []
        dropped = 0
        for pair in pairs.each():
            try:
                distance = pair.distance()
            except Exception:
                distance = None
            if distance == 0:
                dropped += 1
                continue
            kept.append(pair)
        return kept, dropped

    results = []
    for rule in PARAMS.get("rules") or []:
        check = (rule.get("check") or "").lower()
        entry = {"name": rule.get("name") or ("%s on %s" % (check, rule.get("layer"))),
                 "check": check, "layer": rule.get("layer"),
                 "limit_um": rule.get("value"), "violations": 0, "markers": []}
        try:
            if check not in SUPPORTED_CHECKS:
                raise ValueError("unsupported check %r (supported: %s)"
                                 % (check, ", ".join(SUPPORTED_CHECKS)))
            tolerance = to_db(rule["value"], scale)
            dropped = 0
            if check == "width":
                items, dropped = edge_pairs(region_for(rule["layer"]), "width", tolerance)
            elif check == "space":
                items, dropped = edge_pairs(region_for(rule["layer"]), "space", tolerance)
            elif check == "notch":
                items, dropped = edge_pairs(region_for(rule["layer"]), "notch", tolerance)
            elif check == "enclosing":
                other = rule.get("other")
                if not other:
                    raise ValueError("enclosing needs 'other'")
                items, dropped = edge_pairs(region_for(rule["layer"]), "enclosing",
                                            tolerance, other=region_for(other))
            else:  # area
                minimum = float(rule["value"]) / (ly.dbu * ly.dbu)
                small = [polygon for polygon in region_for(rule["layer"]).each()
                         if polygon.area() < minimum]
                count = len(small)
                markers = []
                for polygon in small[:MAX_MARKERS]:
                    box = polygon.bbox()
                    markers.append({"bbox_um": [box.left * ly.dbu, box.bottom * ly.dbu,
                                                box.right * ly.dbu, box.top * ly.dbu]})
                entry["violations"] = count
                entry["markers"] = markers
                entry["touching_edges_ignored"] = 0
                results.append(entry)
                continue
            count, markers = markers_of(items)
            entry["violations"] = count
            entry["markers"] = markers
            entry["touching_edges_ignored"] = dropped
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        results.append(entry)
    total = sum(item.get("violations", 0) for item in results)
    report({"ok": True, "operation": "drc", "path": LAYOUT_PATH, "dbu": ly.dbu,
            "rules": results, "violations": total,
            "clean": total == 0 and not any(item.get("error") for item in results),
            "markers_capped_at": MAX_MARKERS})


def region_of(ly, ref, cell_name):
    index = resolve_layer(ly, ref)
    region = pya.Region()
    cells = [ly.cell(cell_name)] if cell_name else ly.top_cells()
    for cell in cells:
        region += pya.Region(cell.begin_shapes_rec(index))
    return region.merged()


def do_boolean():
    spec = PARAMS["spec"]
    op = (spec.get("op") or "").lower()
    ly = pya.Layout()
    if spec.get("input"):
        ly.read(spec["input"])
    a = region_of(ly, spec["a"], spec.get("cell_a"))
    b = region_of(ly, spec["b"], spec.get("cell_b")) if spec.get("b") else None
    if op == "merge":
        result = a
    elif op in ("and", "intersection"):
        result = a & b
    elif op in ("not", "difference"):
        result = a - b
    elif op == "xor":
        result = a ^ b
    elif op in ("size", "grow", "shrink"):
        amount = to_db(spec["value"], unit_scale(ly))
        if op == "shrink":
            amount = -amount
        result = a.sized(int(amount))
    else:
        raise ValueError("unsupported boolean op %r (merge/and/not/xor/size)" % op)

    out_cell = ly.create_cell(spec.get("out_cell") or "BOOLEAN")
    out_cell.shapes(resolve_layer(ly, spec["out_layer"])).insert(result)
    if spec.get("output"):
        ly.write(spec["output"])
    bbox = result.bbox()
    report({"ok": True, "operation": "boolean", "op": op,
            "path": spec.get("output") or LAYOUT_PATH,
            "polygons": sum(1 for _polygon in result.each()),
            "bbox_um": None if bbox.empty() else
                       [bbox.left * ly.dbu, bbox.bottom * ly.dbu,
                        bbox.right * ly.dbu, bbox.top * ly.dbu]})


def main():
    operation = PARAMS.get("op")
    if operation == "gen":
        do_gen()
    elif operation == "info":
        do_info()
    elif operation == "drc":
        do_drc()
    elif operation == "boolean":
        do_boolean()
    else:
        fail("unsupported operation %r (gen/info/drc/boolean)" % operation)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        fail("klayout script raised", traceback.format_exc())
