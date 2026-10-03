#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live probe: drive Empyrean ALPS through the bridge (vendor simulator flow).

ALPS is the simulator Aether/MDE normally configures in the GUI; this probe
checks that the bridge can drive its **batch CLI** instead. It is opt-in because
it needs a running target with ALPS installed and a **valid Empyrean licence** --
the bridge never touches licensing itself.

The probe distinguishes three outcomes, because they mean different things:

  * ALPS missing            -> SKIP (nothing to check here)
  * ALPS ran and produced data -> the vendor path works end to end
  * ALPS refused to run     -> FAIL only if the bridge is at fault; a licence
                               error or an emulation abort (`Exe = .../rosetta`)
                               is reported as an environment limit, with the
                               evidence quoted, and does not blame the bridge

Usage::

    PYAETHER_ALPS_PROBE=1 python3 tests/alps_live_probe.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


MINIMAL_DIVIDER = """* minimal divider, .op only
v1 in 0 dc 1
r1 in out 1k
r2 out 0 1k
.op
.end
"""


def main():
    from pyaether_bridge import simulators as S

    print("== ALPS live probe (vendor simulator through the bridge) ==")
    if os.environ.get("PYAETHER_ALPS_PROBE") != "1":
        skip("alps live run", "set PYAETHER_ALPS_PROBE=1 to enable "
                              "(needs ALPS on the target and a valid licence)")
        return report()

    info = S.probe("alps")
    print("probe : available=%s target=%s %s"
          % (info["available"], info["target"], info["version"] or info["detail"]))
    if not info["available"]:
        skip("alps live run", "%s" % (info["detail"] or "ALPS not found"))
        return report()

    check("ALPS is discoverable on the bridge target", True)
    result = S.run("minimal_divider.sp", backend="alps", mode="basic", timeout=180,
                   netlist_text=MINIMAL_DIVIDER, run_id="live-probe")
    metadata = result.get("metadata") or {}
    print("status: %s" % result["status"])
    print("command: %s" % metadata.get("command", ""))
    print("errors: %s" % (result["errors"] or [])[:300])

    if result["ok"]:
        check("ALPS ran and returned parseable data", bool(result["data"]),
              "data keys=%s" % sorted(result["data"])[:8])
        check("artifacts were produced", bool(metadata.get("artifacts")),
              str(metadata.get("artifacts"))[:300])
        return report()

    # Not ok: decide whether the bridge or the environment is responsible.
    check("a failing ALPS run is not reported as ok", result["ok"] is False)
    check("the failure carries a classified cause", bool(result["errors"]),
          str(result["errors"])[:200])
    check("the run directory and command are reported for debugging",
          bool(metadata.get("work_dir")) and bool(metadata.get("command")),
          str(metadata)[:200])

    log_path = os.path.join(metadata.get("work_dir", ""), "psf", "sim.log")
    target = S.transport_for_simulator("alps")
    log_text = ""
    try:
        local = tempfile.mkdtemp(prefix="pyaether-alps-")
        local_log = os.path.join(local, "sim.log")
        target.fetch_file(log_path, local_log)
        with open(local_log, encoding="utf-8", errors="replace") as handle:
            log_text = handle.read()
    except Exception as exc:
        print("       (could not fetch the log for attribution: %s)" % exc)

    lower = log_text.lower()
    emulated = "rosetta" in lower or "virtualapple" in lower
    licence = "licen" in lower and ("error" in lower or "denied" in lower)
    if emulated:
        print("       environment limit: ALPS is running under x86_64 emulation")
        for line in log_text.splitlines():
            if "rosetta" in line.lower() or "CPU" in line or "INTERNAL ERROR" in line:
                print("         | %s" % line.strip())
        skip("alps engine execution",
             "the vendor binary aborts under x86_64 emulation (Exe=/run/rosetta); "
             "run the target on a real x86_64 Linux host to exercise it")
        check("the abort is classified as a crash, not as a silent success",
              any("crash" in item.lower() for item in result["errors"]),
              str(result["errors"])[:200])
    elif licence:
        skip("alps engine execution", "no usable licence on the target")
    else:
        check("an unexplained ALPS failure is surfaced with its log location", False,
              "errors=%s log=%s" % (result["errors"], log_path))
    return report()


def report():
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
