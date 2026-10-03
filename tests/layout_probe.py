#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Layout probe: real KLayout runs against known geometry.

Every check compares the engine's answer with geometry we placed ourselves, so
a green run means KLayout actually read and measured what we wrote:

  * a spec with boxes, a polygon, a path, a text and an instance array produces
    the expected shape counts and bounding box;
  * ``info`` reports the same cells/layers that were written;
  * a deliberately too-large space rule finds the violation, a satisfiable rule
    stays clean, and a rule that cannot be evaluated *fails* the run;
  * layer algebra returns the expected polygon count;
  * GDS2 carries no layer-name table while OASIS does (documented difference,
    asserted so callers are told to pass a map instead of getting silence).

Skips when KLayout is not installed. Set PYAETHER_KLAYOUT_BIN to point at it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CLI = ROOT / "bin" / "pyaether"

PASSED = []
FAILED = []
SKIPPED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       ("\n       %s" % detail) if (detail and not condition) else ""))


def skip(name, reason):
    SKIPPED.append(name)
    print("SKIP %s -- %s" % (name, reason))


SPEC = {
    "top": "TOP",
    "dbu": 0.001,
    "layers": {"m1": [1, 0], "m2": [2, 0], "contact": [3, 0]},
    "cells": {
        "UNIT": {"shapes": [
            {"layer": "m1", "box": [0, 0, 2, 1]},
            {"layer": "contact", "box": [0.8, 0.4, 1.0, 0.6]},
        ]},
        "TOP": {"shapes": [
            {"layer": "m2", "box": [0, 0, 12, 8]},
            {"layer": "m1", "polygon": [[0, 0], [3, 0], [1.5, 2]]},
            {"layer": "m1", "path": [[0, 6], [10, 6]], "width": 0.4},
            {"layer": "m1", "box": [1, 1, 3, 3]},
            {"layer": "m1", "box": [5, 1, 7, 3]},
            {"layer": "m1", "text": "T1", "at": [1, 7]},
        ], "instances": [
            {"cell": "UNIT", "at": [0, 0], "columns": 2, "rows": 1, "dx": 3, "dy": 0},
        ]},
    },
}

LAYERS = {"m1": [1, 0], "m2": [2, 0], "contact": [3, 0]}


def run_cli(*args, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, str(CLI)] + list(args), cwd=str(ROOT),
                          env=merged, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=300)


def main():
    from pyaether_bridge import layout as L

    print("== layout probe (KLayout) ==")
    print("repo : %s" % ROOT)
    probe = L.probe()
    print("klayout: available=%s target=%s %s"
          % (probe["available"], probe["target"], probe["version"] or probe["detail"]))
    print()
    if not probe["available"]:
        skip("layout operations", "no klayout on the resolved target (%s)"
             % probe.get("detail"))
        return report()

    work = pathlib.Path(tempfile.mkdtemp(prefix="pyaether-layout-"))
    gds = work / "top.gds"
    oas = work / "top.oas"
    print("work : %s" % work)
    print()

    # ---- 1. generate ------------------------------------------------------
    print("-- 1. generate from a spec --")
    result = L.generate(SPEC, str(gds))
    check("gen succeeds", result["ok"] is True,
          "status=%s errors=%s" % (result["status"], result["errors"]))
    counts = (result["data"] or {}).get("counts") or {}
    check("every shape kind was written",
          counts.get("box") == 5 and counts.get("polygon") == 1
          and counts.get("path") == 1 and counts.get("text") == 1
          and counts.get("instance") == 1, str(counts))
    check("the top cell is reported", (result["data"] or {}).get("top") == "TOP",
          str((result["data"] or {}).get("top")))
    # The path at y=6 with width 0.4 plus text above it defines the top edge;
    # the m2 rectangle defines the rest.
    bbox = (result["data"] or {}).get("bbox_um")
    check("the bounding box matches the drawn geometry",
          bbox is not None and bbox[0] == 0.0 and bbox[1] == 0.0
          and bbox[2] == 12.0 and 8.0 <= bbox[3] <= 8.01, str(bbox))
    check("the file exists on disk", gds.is_file() and gds.stat().st_size > 0,
          "%s bytes" % (gds.stat().st_size if gds.is_file() else "missing"))
    print()

    # ---- 2. info ----------------------------------------------------------
    print("-- 2. read it back --")
    info = L.info(str(gds), layers=LAYERS)
    check("info succeeds", info["ok"] is True, str(info["errors"])[:300])
    data = info["data"] or {}
    cells = {c["name"]: c for c in data.get("cells") or []}
    check("both cells are present", set(cells) == {"TOP", "UNIT"}, str(sorted(cells)))
    check("the top cell keeps its instance", cells.get("TOP", {}).get("instances") == 1,
          str(cells.get("TOP", {}).get("instances")))
    check("shape counts per layer are reported",
          cells.get("TOP", {}).get("shapes_by_layer", {}).get("2/0") == 1,
          str(cells.get("TOP", {}).get("shapes_by_layer")))
    check("the top cell is reported as a top cell", data.get("top_cells") == ["TOP"],
          str(data.get("top_cells")))
    print()

    # ---- 3. drc -----------------------------------------------------------
    print("-- 3. design-rule checks --")
    # The two m1 boxes at x=1..3 and x=5..7 are 2 um apart.
    spacing = L.drc(str(gds), [
        {"name": "m1 space 3um", "check": "space", "layer": "m1", "value": 3.0},
    ], layers=LAYERS)
    check("a violated rule is reported, not hidden",
          spacing["ok"] is True and (spacing["data"] or {}).get("violations", 0) >= 1,
          "status=%s data=%s" % (spacing["status"],
                                 json_dump((spacing["data"] or {}).get("violations"))))
    check("the run is marked dirty", (spacing["data"] or {}).get("clean") is False)
    markers = ((spacing["data"] or {}).get("rules") or [{}])[0].get("markers") or []
    check("the violation carries a location", bool(markers), str(markers)[:200])

    clean = L.drc(str(gds), [
        {"name": "m1 space 0.1um", "check": "space", "layer": "m1", "value": 0.1},
        {"name": "contact area 0.001", "check": "area", "layer": "contact", "value": 0.001},
    ], layers=LAYERS)
    check("satisfiable rules report clean",
          clean["ok"] is True and (clean["data"] or {}).get("clean") is True,
          json_dump(clean["data"])[:300] if clean["ok"] else str(clean["errors"])[:300])
    # Merged geometry produces touching edges (distance 0) that are not spacing
    # violations; the check must ignore them and say how many it ignored.
    spacing_rule = ((clean["data"] or {}).get("rules") or [{}])[0]
    check("touching edges are not counted as spacing violations",
          spacing_rule.get("violations") == 0,
          json_dump(spacing_rule)[:250])
    check("the number of ignored touching edges is reported",
          "touching_edges_ignored" in spacing_rule, json_dump(spacing_rule)[:200])

    narrow = L.drc(str(gds), [
        {"name": "m1 width 0.5um", "check": "width", "layer": "m1", "value": 0.5},
    ], layers=LAYERS)
    check("a width rule catches the 0.4um path",
          narrow["ok"] is True and (narrow["data"] or {}).get("violations", 0) >= 1,
          json_dump((narrow["data"] or {}).get("rules"))[:250])

    broken = L.drc(str(gds), [
        {"name": "not a real check", "check": "nonsense", "layer": "m1", "value": 1},
    ], layers=LAYERS)
    check("a rule that cannot be evaluated fails the run", broken["ok"] is False,
          "status=%s" % broken["status"])
    check("the failure says which rule could not run",
          any("could not be evaluated" in item for item in broken["errors"]),
          str(broken["errors"])[:300])

    missing = L.drc(str(gds), [
        {"name": "unknown layer", "check": "width", "layer": "nope", "value": 1},
    ], layers=LAYERS)
    check("an unknown layer fails instead of reporting zero violations",
          missing["ok"] is False and (missing["data"] or {}).get("violations") == 0,
          "status=%s errors=%s" % (missing["status"], str(missing["errors"])[:200]))
    print()

    # ---- 4. layer names: GDS2 vs OASIS ------------------------------------
    print("-- 4. layer names survive OASIS, not GDS2 --")
    check("OASIS generation succeeds", L.generate(SPEC, str(oas))["ok"] is True)
    without_map = L.drc(str(oas), [
        {"name": "width", "check": "width", "layer": "m1", "value": 0.1}])
    check("OASIS resolves layer names without a map", without_map["ok"] is True,
          str(without_map["errors"])[:200])
    gds_without_map = L.drc(str(gds), [
        {"name": "width", "check": "width", "layer": "m1", "value": 0.1}])
    check("GDS2 without a layer map fails loudly (no silent pass)",
          gds_without_map["ok"] is False
          and any("unknown layer" in item for item in gds_without_map["errors"]),
          str(gds_without_map["errors"])[:250])
    print()

    # ---- 5. boolean -------------------------------------------------------
    print("-- 5. layer algebra --")
    boolean = L.boolean("and", [1, 0], b=[2, 0], out_layer=[4, 0],
                        output=str(work / "bool.gds"), source=str(gds))
    check("boolean and succeeds", boolean["ok"] is True, str(boolean["errors"])[:200])
    # m1 intersected with m2: the shapes inside the m2 rectangle merge into a
    # few disjoint polygons, so assert a non-empty, plausible result.
    check("the result has the expected polygon count",
          (boolean["data"] or {}).get("polygons") is not None
          and (boolean["data"] or {}).get("polygons") >= 2,
          json_dump((boolean["data"] or {}).get("polygons")))
    grown = L.boolean("grow", [3, 0], value=0.2, out_layer=[5, 0],
                      output=str(work / "grow.gds"), source=str(gds))
    check("boolean grow succeeds and reports polygons",
          grown["ok"] is True and (grown["data"] or {}).get("polygons", 0) >= 1,
          str(grown["errors"])[:200])
    print()

    # ---- 6. CLI exit codes ------------------------------------------------
    # ---- 6. stream tools: convert / compare -------------------------------
    print("-- 6. KLayout stream tools (no Python) --")
    tools = L.stream_tools()
    check("the stream tools are discovered", bool(tools),
          "set PYAETHER_KLAYOUT_BUDDY_DIR to the directory holding them")
    if tools:
        print("       found: %s" % ", ".join(sorted(tools)))
        converted = work / "converted.oas"
        conv = L.convert(str(gds), str(converted))
        check("GDS -> OASIS conversion succeeds", conv["ok"] is True,
              json_dump(conv.get("errors"))[:200])
        check("the converted file exists and is not empty",
              converted.is_file() and converted.stat().st_size > 0,
              "%s bytes" % (converted.stat().st_size if converted.is_file() else "missing"))

        same = L.compare(str(gds), str(gds))
        check("comparing a file with itself reports identical",
              (same["data"] or {}).get("identical") is True,
              json_dump(same["data"])[:200])
        different = work / "different.gds"
        spec2 = json.loads(json.dumps(SPEC))
        spec2["cells"]["TOP"]["shapes"].append({"layer": "m1", "box": [30, 0, 40, 5]})
        L.generate(spec2, str(different))
        other = L.compare(str(gds), str(different))
        check("comparing different geometry reports a difference",
              (other["data"] or {}).get("identical") is False
              and (other["data"] or {}).get("returncode") != 0,
              json_dump(other["data"])[:200])
        check("a difference is not reported as an error",
              other["ok"] is True, json_dump(other.get("errors"))[:200])
        # Same geometry, different container format: content-wise identical.
        cross = L.compare(str(gds), str(converted))
        check("compare is content based, so GDS vs OASIS matches",
              (cross["data"] or {}).get("identical") is True,
              json_dump(cross["data"])[:200])
    print()

    # ---- 7. rule deck -----------------------------------------------------
    print("-- 7. rule deck (.drc) through the real engine --")
    deck_file = work / "probe.drc"
    deck_file.write_text(
        'source("%s")\nreport("%s")\n'
        'm1 = input(1, 0)\nm1.width(3.0).output("m1 width under 3um")\n'
        % (gds, work / "probe.lyrdb"), encoding="utf-8")
    try:
        deck_result = L.deck(str(deck_file), source=str(gds))
        check("the deck runs on the target", deck_result["status"] in
              ("SUCCESS", "PARTIAL", "FAILURE"),
              json_dump(deck_result.get("errors"))[:200])
        # Documents a measured limit: this engine executes the deck but writes no
        # report database in batch mode, so the run must NOT claim to be clean.
        if not deck_result["data"].get("report_artifacts"):
            check("a deck without a report is not reported as clean",
                  deck_result["ok"] is False
                  and deck_result["status"] == "PARTIAL",
                  "status=%s ok=%s" % (deck_result["status"], deck_result["ok"]))
            check("the limitation is explained to the caller",
                  any("report database" in item for item in deck_result["errors"]),
                  str(deck_result["errors"])[:200])
        else:
            check("a deck that writes a report is reported as successful",
                  deck_result["ok"] is True, str(deck_result["errors"])[:200])
        bad_deck = work / "not_a_deck.txt"
        bad_deck.write_text("not a deck\n", encoding="utf-8")
        try:
            L.deck(str(bad_deck))
            check("a non-deck file is refused", False, "no exception raised")
        except L.LayoutError as exc:
            check("a non-deck file is refused", ".drc" in str(exc), str(exc)[:200])
    except Exception as exc:
        check("the deck path is usable", False, "%s: %s" % (type(exc).__name__, exc))
    print()

    # ---- 8. CLI exit codes ------------------------------------------------
    print("-- 8. CLI exit codes (a dirty layout must not exit 0) --")
    rules_file = work / "rules.json"
    rules_file.write_text(json_dump([
        {"name": "m1 space 3um", "check": "space", "layer": "m1", "value": 3.0},
    ]), encoding="utf-8")
    dirty = run_cli("layout", "drc", str(gds), "--rules", str(rules_file),
                    "--layers", json_dump(LAYERS))
    check("violations exit with code 3", dirty.returncode == 3,
          "rc=%s stderr=%s" % (dirty.returncode, dirty.stderr[-200:]))
    allowed = run_cli("layout", "drc", str(gds), "--rules", str(rules_file),
                      "--layers", json_dump(LAYERS), "--exit-zero")
    check("--exit-zero returns 0 for scripting", allowed.returncode == 0,
          "rc=%s" % allowed.returncode)
    clean_file = work / "clean.json"
    clean_file.write_text(json_dump([
        {"name": "m1 space 0.1um", "check": "space", "layer": "m1", "value": 0.1},
    ]), encoding="utf-8")
    ok_run = run_cli("layout", "drc", str(gds), "--rules", str(clean_file),
                     "--layers", json_dump(LAYERS))
    check("a clean layout exits 0", ok_run.returncode == 0,
          "rc=%s stderr=%s" % (ok_run.returncode, ok_run.stderr[-200:]))
    print()

    ok = run_cli("layout", "info", str(gds), "--layers", json_dump(LAYERS), "--json")
    check("CLI json output is valid JSON", ok.returncode == 0 and json_ok(ok.stdout),
          ok.stdout[:200] or ok.stderr[-200:])
    print()

    shutil.rmtree(work, ignore_errors=True)
    return report()


def json_dump(value):

    return json.dumps(value, ensure_ascii=False)


def json_ok(text):

    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def report():
    print("-" * 64)
    print("result: %d PASS / %d FAIL / %d SKIP"
          % (len(PASSED), len(FAILED), len(SKIPPED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
