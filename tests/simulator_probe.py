#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simulator layer probe: real ngspice run plus backend contract checks.

Covers:
  * a real ngspice run on this machine (RC low-pass, AC analysis) whose -3 dB
    point is checked against 1/(2*pi*R*C) -- the numbers come from the
    simulator, not from a fixture
  * failure classification (a broken netlist must not look like a success)
  * command construction for Spectre modes (no Spectre binary required)
  * the strict accessors refusing to invent data

Usage::

    python3 tests/simulator_probe.py

Set PYAETHER_NGSPICE_BIN if ngspice is not on PATH. Set
PYAETHER_SIM_TARGET=local to run simulators on this machine (the probe does
that itself, so it works even when the bridge targets a container).
"""

from __future__ import annotations

import math
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       ("\n       %s" % detail) if (detail and not condition) else ""))


RC_NETLIST = """* RC low-pass, AC analysis
V1 in 0 dc 1 ac 1
R1 in out 1k
C1 out 0 1u
.ac dec 20 1 1meg
.end
"""

BROKEN_NETLIST = """* missing device node -> read error
V1 in 0 dc 1
R1 in out 1k
XX_NOT_A_DEVICE out 0 FOO
.ac dec 5 1 1k
.end
"""


def main():
    os.environ["PYAETHER_SIM_TARGET"] = "local"
    os.environ.setdefault("PYAETHER_SIM_WORKDIR",
                          tempfile.mkdtemp(prefix="pyaether-sim-probe-"))
    # Re-import so the config above is picked up.
    for module in [name for name in sys.modules if name.startswith("pyaether_bridge")]:
        del sys.modules[module]
    from pyaether_bridge import config, simulators as S  # noqa: E402

    print("== simulator layer probe ==")
    print("python    : %s (%s)" % (sys.executable, sys.version.split()[0]))
    print("workdir   : %s" % config.SIM_WORKDIR)
    print("backend   : %s" % config.SIM_BACKEND)
    print()

    # ---- 1. probe --------------------------------------------------------
    print("-- 1. backend probing --")
    for backend in ("ngspice", "spectre", "custom"):
        info = S.probe(backend)
        print("   %-8s available=%-5s target=%-6s %s"
              % (backend, info["available"], info["target"],
                 (info["version"] or info["detail"])[:70]))
    ngspice = shutil.which(config.NGSPICE_BIN) or shutil.which("ngspice")
    check("ngspice is installed (open-source EDA path is testable)",
          bool(ngspice), "no ngspice on PATH; install it or set PYAETHER_NGSPICE_BIN")
    if not ngspice:
        return finish()
    print()

    # ---- 2. real run -----------------------------------------------------
    print("-- 2. real ngspice run (RC low-pass AC) --")
    result = S.run("rc_lowpass.cir", backend="ngspice", netlist_text=RC_NETLIST,
                   timeout=120, run_id="probe-ac")
    check("run reports ok", result["ok"] is True,
          "status=%s errors=%s" % (result["status"], result["errors"]))
    check("status is SUCCESS", result["status"] == "SUCCESS", result["status"])
    check("returncode is 0", result["metadata"].get("returncode") == 0,
          str(result["metadata"].get("returncode")))
    check("parsed a plot named 'AC Analysis'",
          any("AC" in plot["name"] for plot in result["metadata"].get("plots", [])),
          str(result["metadata"].get("plots")))

    data = result["data"]
    check("frequency vector present", "frequency" in data, str(sorted(data))[:200])
    check("output node vector present", "v(out)" in data, str(sorted(data))[:200])

    if "frequency" in data and "v(out)" in data:
        freqs = S.vector(data, "frequency")
        out = S.vector(data, "v(out)")
        check("frequency and v(out) have equal length", len(freqs) == len(out),
              "%d vs %d" % (len(freqs), len(out)))
        mags = [abs(value) for value in out]
        # A 1 kHz low-pass driven by a 1 V AC source: DC gain 1, unity at the
        # corner, -20 dB/decade beyond it.
        dc_gain = mags[0]
        check("DC gain is ~1 V", abs(dc_gain - 1.0) < 0.02, "dc gain=%.6f" % dc_gain)
        corner = 1.0 / (2.0 * math.pi * 1e3 * 1e-6)
        check("corner frequency matches 1/(2*pi*R*C) = 159.15 Hz",
              abs(corner - 159.1549) < 0.01, "corner=%.4f" % corner)
        # -3 dB = 1/sqrt(2) at the corner: interpolate on the log-frequency axis.
        target = 1.0 / math.sqrt(2.0)
        measured = None
        for index in range(1, len(freqs)):
            if mags[index] <= target <= mags[index - 1]:
                f0, f1 = freqs[index - 1].real, freqs[index].real
                m0, m1 = mags[index - 1], mags[index]
                ratio = (m0 - target) / (m0 - m1) if m0 != m1 else 0.0
                measured = f0 * (f1 / f0) ** ratio
                break
        check("measured -3 dB point is within 5%% of theory",
              measured is not None and abs(measured - corner) / corner < 0.05,
              "measured=%s theory=%.2f" % (measured, corner))
        # Roll-off over the last decade should be about -20 dB.
        if len(freqs) > 10:
            tail = 20.0 * math.log10(mags[-1] / mags[-len(mags) // 2])
            check("high-frequency roll-off is steeper than -15 dB over the swept tail",
                  tail < -15.0, "tail=%.2f dB" % tail)
    check("timings are reported",
          "execution_s" in result["metadata"].get("timings", {}),
          str(result["metadata"].get("timings")))
    print()

    # ---- 3. failure classification ---------------------------------------
    print("-- 3. failure must not look like success --")
    broken = S.run("broken.cir", backend="ngspice", netlist_text=BROKEN_NETLIST,
                   timeout=120, run_id="probe-broken")
    check("broken netlist does not report ok", broken["ok"] is False,
          "status=%s data_keys=%s" % (broken["status"], sorted(broken["data"])[:8]))
    check("broken netlist status is FAILURE or PARTIAL",
          broken["status"] in ("FAILURE", "PARTIAL"), broken["status"])
    check("broken netlist yields classified errors", bool(broken["errors"]),
          str(broken["errors"]))
    print("       errors: %s" % (broken["errors"] or "[]")[:200])
    check("the 'warnings go to log-file' banner is not reported as a warning",
          not any("go to" in item.lower() for item in broken["warnings"]),
          str(broken["warnings"]))
    print()

    # ---- 3b. target resolution -------------------------------------------
    print("-- 3b. which machine runs which backend --")
    saved_target = config.SIM_TARGET
    try:
        config.SIM_TARGET = ""
        kind, reason = S.resolve_target("ngspice")
        check("unset SIM_TARGET + local ngspice resolves to this machine",
              kind == "local", "kind=%s reason=%s" % (kind, reason))
        check("the resolution reason is reported", bool(reason), reason)
        kind, reason = S.resolve_target("spectre")
        check("unset SIM_TARGET + spectre follows the bridge target",
              kind == (config.TRANSPORT or "").lower(),
              "kind=%s transport=%s" % (kind, config.TRANSPORT))
        config.SIM_TARGET = "ssh"
        expected = "ssh"
        got, reason = S.resolve_target("ngspice")
        check("an explicit SIM_TARGET always wins", got == expected,
              "kind=%s reason=%s" % (got, reason))
    finally:
        config.SIM_TARGET = saved_target
    print()

    # ---- 4. Spectre command construction (no binary needed) --------------
    print("-- 4. Spectre modes (command construction only) --")
    for mode in ("spectre", "aps", "ax"):
        command, env = S.build_command("spectre", netlist="/w/tb.scs", workdir="/w",
                                       log="/w/sim.log", raw="/w/raw", mode=mode)
        check("mode %-7s builds a command with -format psfascii" % mode,
              "-format psfascii" in command and "/w/tb.scs" in command, command[:160])
    ax_command, _ = S.build_command("spectre", netlist="/w/tb.scs", workdir="/w",
                                    log="/w/sim.log", raw="/w/raw", mode="ax")
    check("ax mode selects the preset engine", "+preset=ax" in ax_command, ax_command[:160])
    aps_command, _ = S.build_command("spectre", netlist="/w/tb.scs", workdir="/w",
                                     log="/w/sim.log", raw="/w/raw", mode="aps")
    check("aps mode adds +aps", "+aps" in aps_command, aps_command[:160])
    plus_command, _ = S.build_command("spectre", netlist="/w/tb.scs", workdir="/w",
                                      log="/w/sim.log", raw="/w/raw", mode="aps-plus")
    check("aps-plus mode uses the distinct ++aps flag", "++aps" in plus_command,
          plus_command[:160])
    conservative, _ = S.build_command("spectre", netlist="/w/tb.scs", workdir="/w",
                                      log="/w/sim.log", raw="/w/raw",
                                      mode="aps-conservative")
    check("aps accuracy presets are passed explicitly",
          "+aps=conservative" in conservative, conservative[:160])
    try:
        S.build_command("spectre", netlist="n", workdir="w", log="l", raw="r", mode="nope")
        check("unknown Spectre mode is rejected", False, "no exception raised")
    except S.SimulatorError as exc:
        check("unknown Spectre mode is rejected", "unsupported Spectre mode" in str(exc), str(exc))
    check("every Spectre mode carries a documented note",
          set(S.SPECTRE_MODES) <= set(S.SPECTRE_MODE_NOTES),
          str(sorted(set(S.SPECTRE_MODES) - set(S.SPECTRE_MODE_NOTES))))
    custom, _ = S.build_command("custom", netlist="/w/n.cir", workdir="/w",
                                log="/w/l.log", raw="/w/r.raw", mode="tran",
                                args=["-x"]) if config.SIM_CMD else (None, None)
    check("custom backend demands a configured template", custom is None,
          "PYAETHER_SIM_CMD unexpectedly set")
    print()

    # ---- 5. strict accessors ---------------------------------------------
    print("-- 5. strict accessors --")
    try:
        S.scalar({"a": "1.0"}, "a")
        check("scalar() rejects a string", False, "no exception")
    except ValueError:
        check("scalar() rejects a string", True)
    try:
        S.scalar({}, "missing")
        check("scalar() rejects a missing key", False, "no exception")
    except ValueError:
        check("scalar() rejects a missing key", True)
    try:
        S.vector({"a": []}, "a")
        check("vector() rejects an empty vector", False, "no exception")
    except ValueError:
        check("vector() rejects an empty vector", True)
    try:
        S.scalar({"a": float("nan")}, "a")
        check("scalar() rejects NaN", False, "no exception")
    except ValueError:
        check("scalar() rejects NaN", True)
    print()

    return finish()


def finish():
    print("-" * 64)
    print("result: %d PASS / %d FAIL" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
