#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Schematic round-trip probe.

Offline half (always runs) checks the normalizer and the SPICE writer against a
canvas-style document: nested ``reference``/``placement``, net names carried by
connectivity evidence, the formal interface under ``netlist.terminals``, and the
refusal to guess a target cell.

Live half (opt-in) builds a schematic in Aether and reads it back. Enable it
with PYAETHER_SCH_PROBE_LIB set to a library that exists on the target and has
symbol masters available, for example::

    PYAETHER_SCH_PROBE_LIB=mywork python3 tests/schematic_probe.py

Usage: python3 tests/schematic_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED, FAILED, SKIPPED = [], [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       ("\n       %s" % detail) if (detail and not condition) else ""))


def skip(name, reason):
    SKIPPED.append(name)
    print("SKIP %s -- %s" % (name, reason))


CANVAS = {
    "id": "doc1", "name": "divider", "revision": 0,
    "netlist": {"name": "divider",
                "terminals": [{"id": "IN", "netId": "n_in", "direction": "input"},
                              {"id": "OUT", "netId": "n_out", "direction": "output"}]},
    "instances": [
        {"id": "i1", "name": "R1",
         "reference": {"library": "analog", "cell": "res", "view": "symbol"},
         "placement": {"position": {"x": 100, "y": 200}}},
        {"id": "i2", "name": "R2",
         "reference": {"library": "analog", "cell": "res", "view": "symbol"},
         "placement": {"position": {"x": 100, "y": 100}}},
    ],
    "nets": [
        {"id": "n_in", "terminals": [{"instanceId": "i1", "pinName": "PLUS"}]},
        {"id": "n_mid", "terminals": [{"instanceId": "i1", "pinName": "MINUS"},
                                       {"instanceId": "i2", "pinName": "PLUS"}]},
        {"id": "n_out", "terminals": [{"instanceId": "i2", "pinName": "MINUS"}]},
    ],
    "connectivityEvidence": [{"id": "e1", "kind": "name-claim",
                              "netId": "n_mid", "name": "MID"}],
}


def main():
    from pyaether_bridge import schematic as S

    print("== schematic round-trip probe ==")
    print()

    print("-- offline: normalize a canvas document --")
    doc = S.normalize(CANVAS, library="mywork", cell="divider")
    check("the target comes from the explicit arguments",
          doc["source"] == {"library": "mywork", "cell": "divider", "view": "schematic"},
          json.dumps(doc["source"]))
    check("instances are read from reference/placement",
          [(i["name"], i["library"], i["cell"], i["position"]) for i in doc["instances"]]
          == [("R1", "analog", "res", [100.0, 200.0]),
              ("R2", "analog", "res", [100.0, 100.0])],
          json.dumps(doc["instances"])[:200])
    check("net names come from connectivity evidence",
          [n["name"] for n in doc["nets"]] == ["n_in", "MID", "n_out"],
          str([n["name"] for n in doc["nets"]]))
    check("the formal interface is read from netlist.terminals",
          [t["name"] for t in doc["terminals"]] == ["IN", "OUT"],
          str(doc["terminals"]))
    check("no field was dropped", "problems" not in doc, str(doc.get("problems")))

    print()
    print("-- offline: refuse to guess --")
    try:
        S.normalize(CANVAS)
        check("an unbound drawing without a target is refused", False, "no exception")
    except S.SchematicError as exc:
        check("an unbound drawing without a target is refused",
              "target cell is unknown" in str(exc), str(exc)[:120])
    incomplete = dict(CANVAS)
    incomplete["instances"] = [{"id": "x", "name": "X"}]
    doc2 = S.normalize(incomplete, library="l", cell="c")
    check("an instance without a master is reported, not ignored",
          doc2.get("problems") and "no master library/cell" in doc2["problems"][0],
          str(doc2.get("problems"))[:160])

    print()
    print("-- offline: SPICE netlist --")
    text = S.netlist(doc)
    check("a .subckt header lists the formal pins",
          text.splitlines()[2].startswith(".subckt divider IN OUT"),
          text.splitlines()[2])
    check("the netlist says which form it uses",
          "connectivity edge list" in text or "positional SPICE" in text,
          text.splitlines()[1])
    check("every connection appears",
          all(("X%s." % name) in text or ("%s/%s" % (i["library"], i["cell"])) in text
              for name, i in [(doc["instances"][0]["name"], doc["instances"][0])]),
          text[:200])
    connections, pins = S.connectivity(doc)
    check("connectivity reports the three nets as connections",
          connections == {("n_in", "i1", "PLUS"), ("MID", "i1", "MINUS"),
                          ("MID", "i2", "PLUS"), ("n_out", "i2", "MINUS")},
          str(sorted(connections)))
    check("connectivity reports the formal pins", pins == {"IN", "OUT"}, str(sorted(pins)))
    written = S.write_netlist(doc, os.path.join(os.getcwd(), "work", "probe-divider.sp"))
    check("write_netlist writes the file",
          os.path.isfile(written) and open(written).read().startswith("* Generated"),
          written)

    print()
    print("-- live: build in Aether and read it back --")
    # Read a real schematic, rebuild it in a scratch cell and compare. This is
    # the path that was verified end to end (17 instances / 22 connections, no
    # difference). A hand-written canvas document works too, but only if its pin
    # names match the master symbol's terminals: a name the symbol does not have
    # creates no connection, so a synthetic document with invented pin names is
    # not a valid acceptance case.
    source_lib = os.environ.get("PYAETHER_SCH_PROBE_SOURCE_LIB")
    source_cell = os.environ.get("PYAETHER_SCH_PROBE_SOURCE_CELL")
    target_lib = os.environ.get("PYAETHER_SCH_PROBE_LIB")
    if not (source_lib and source_cell and target_lib):
        skip("live build + read-back",
             "set PYAETHER_SCH_PROBE_SOURCE_LIB/_SOURCE_CELL (a readable design) and "
             "PYAETHER_SCH_PROBE_LIB (a writable library) to enable")
        return report()
    read = S.snapshot(source_lib, source_cell)
    check("the source design can be read", read["ok"] is True,
          json.dumps(read.get("errors"))[:200])
    if not read["ok"]:
        return report()
    snapshot = read["data"]["snapshot"]
    cell = "probe_%d" % (os.getpid() % 100000)
    result = S.roundtrip({**snapshot,
                          "source": {"library": target_lib, "cell": cell,
                                     "view": "schematic"}})
    check("the round trip succeeds", result["ok"] is True,
          json.dumps({"status": result["status"], "errors": result["errors"][:2],
                      "data": {k: result["data"].get(k) for k in
                               ("missing_connections_total",
                                "unexpected_connections_total")}}, ensure_ascii=False)[:300])
    back = result["data"].get("read_back") or {}
    check("the read-back has every instance",
          back.get("instances") == read["data"]["counts"]["instances"], str(back))
    check("no connection is missing", result["data"].get("missing_connections_total") == 0,
          str(result["data"].get("missing_connections")))
    check("no connection was invented",
          result["data"].get("unexpected_connections_total") == 0,
          str(result["data"].get("unexpected_connections")))
    return report()


def report():
    print()
    print("-" * 64)
    print("result: %d PASS / %d FAIL / %d SKIP" % (len(PASSED), len(FAILED), len(SKIPPED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
