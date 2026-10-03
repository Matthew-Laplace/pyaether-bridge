# -*- coding: utf-8 -*-
"""Simulator-agnostic SPICE runner: ngspice (open source), Spectre, or your own CLI.

Why this module exists: the same netlist should be runnable on whichever
simulator is available and licensed -- open-source ngspice on a laptop, Cadence
Spectre/APS on a lab server, or a vendor CLI wired in through a command
template. The bridge must not care which one is used, and the results must stay
comparable.

Design rules (this is why the results are trustworthy):
  1. ``result["ok"]`` is the execution contract. A non-empty ``data`` dict is
     NOT proof of success -- a crashed run can still leave partial raw files.
  2. Failures are classified into short, actionable causes (netlist read error /
     license error / convergence failure / missing file / crash) instead of
     dumping raw log text at the caller.
  3. Every run gets its own work directory, so runs cannot read each other's
     logs or raw files.
  4. Parsed data is only reported when the parser actually understood the file;
     otherwise the status is PARTIAL and the caller is told what artifacts exist.

Backends:
  ngspice   open source; batch mode, ASCII rawfile via SPICE_ASCIIRAWFILE=1
  spectre   Cadence Spectre / APS / AX ...; PSF-ASCII output (mode flag sets
            kept identical to Arcadia-1/virtuoso-bridge-lite so runs compare)
  custom    any other simulator through a command template, e.g. a vendor or
            in-house simulator whose flags we must not guess
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import tempfile
import time
import uuid

from . import config, transports

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILURE = "FAILURE"

# Spectre flag sets. The preset entries (spectre/aps/x/cx/ax/mx/lx/vx) match
# Arcadia-1/virtuoso-bridge-lite so results from either bridge stay comparable;
# "+mt" enables the multithreaded engine for the preset-based modes. The
# aps-plus / aps-<precision> entries come from the Spectre 18.1 user guide and
# are additions, not upstream flags.
SPECTRE_MODES = {
    "spectre": [],
    "aps": ["+aps"],
    # Spectre 18.1 also documents a distinct "+ + aps" mode (here: "aps-plus")
    # and explicit accuracy presets for +aps. Keep both explicit rather than
    # inventing flag combinations.
    "aps-plus": ["++aps"],
    "aps-liberal": ["+aps=liberal"],
    "aps-moderate": ["+aps=moderate"],
    "aps-conservative": ["+aps=conservative"],
    "x": ["+x"],
    "cx": ["+preset=cx", "+mt"],
    "ax": ["+preset=ax", "+mt"],
    "mx": ["+preset=mx", "+mt"],
    "lx": ["+preset=lx", "+mt"],
    "vx": ["+preset=vx", "+mt"],
}

# Measured speed / accuracy trade-off for the preset engines (11-bit sub-radix-2
# SAR ADC, transient, N=128 coherent FFT, ax baseline ~220 s). ENOB is relative
# to the +aps reference. Source: the local `spectre` skill, which derives these
# from the Spectre 18.1 user guide plus measured runs. Use them to pick a mode
# deliberately -- "lx"/"vx" are unusable for circuits with comparators/regeneration.
SPECTRE_MODE_NOTES = {
    "spectre": "slowest, lowest license demand, no preset",
    "aps": "1.0x speed, reference accuracy",
    "aps-plus": "distinct APS mode with different time-step/Newton controls",
    "aps-liberal": "+aps with relaxed accuracy controls",
    "aps-moderate": "+aps with moderate accuracy controls",
    "aps-conservative": "+aps with conservative accuracy controls",
    "cx": "1.2x speed, ENOB -0.03 (stiff mixed-signal loops)",
    "ax": "2.0x speed, ENOB -0.03 (good daily default)",
    "mx": "3.8x speed, ENOB -0.29 (exploration, corner sweeps)",
    "lx": "5.9x speed, ENOB -2.8 (AC/linear DC only, not for comparator circuits)",
    "vx": "8.8x speed, ENOB -8.5 (connectivity/DC convergence only)",
    "x": "Spectre X",
}

# Informational: for ngspice the analysis lives inside the netlist.
NGSPICE_MODES = ["tran", "ac", "dc", "op", "noise"]


class SimulatorError(RuntimeError):
    """Unsupported backend, unusable configuration, or a staging failure."""


def backends():
    """Static description of the available backends (no probing)."""
    return {
        "ngspice": {
            "kind": "open-source",
            "binary_config": "PYAETHER_NGSPICE_BIN",
            "default_binary": "ngspice",
            "modes": NGSPICE_MODES,
            "notes": "Analysis is declared inside the netlist "
                     "(.tran/.ac/.dc/.op/.noise); results come from an ASCII rawfile.",
        },
        "spectre": {
            "kind": "commercial",
            "binary_config": "PYAETHER_SPECTRE_BIN",
            "default_binary": "spectre",
            "modes": sorted(SPECTRE_MODES),
            "mode_notes": SPECTRE_MODE_NOTES,
            "notes": "Modes select the analysis engine or preset "
                     "(aps / ax / mx / ...); results are read from PSF-ASCII output.",
        },
        "custom": {
            "kind": "any",
            "binary_config": "PYAETHER_SIM_CMD",
            "default_binary": "",
            "modes": [],
            "notes": "Command template with {netlist} {workdir} {log} {raw} {mode} "
                     "placeholders. Use this for a simulator whose flags you know; "
                     "the bridge refuses to guess flags for an unknown tool.",
        },
    }


def _result(status, backend, *, data=None, errors=None, warnings=None, metadata=None):
    return {
        "ok": status == STATUS_SUCCESS,
        "status": status,
        "backend": backend,
        "data": data or {},
        "errors": list(errors or []),
        "warnings": list(warnings or []),
        "metadata": metadata or {},
    }


def _classify(output):
    """Turn raw simulator output into short causes plus a warning list."""
    errors = []
    warnings = []
    text = output or ""
    lower = text.lower()

    if "error reading" in lower or "read-in failed" in lower or "missing include" in lower:
        errors.append("netlist read error (missing include or syntax)")
    elif "license" in lower and ("error" in lower or "denied" in lower or "cannot" in lower):
        errors.append("license error")
    elif ("failed to converge" in lower or "convergence failed" in lower
          or "convergence failure" in lower or "no convergence" in lower
          or "spcrtrf-15044" in lower or "iteration limit reached" in lower):
        errors.append("convergence failure")
    elif "no such file" in lower or "cannot open" in lower or "can't open" in lower:
        errors.append("file not found")
    elif "segmentation" in lower or "core dumped" in lower or "fatal error" in lower:
        errors.append("simulator crashed")

    if not errors:
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if (lowered.startswith("error") or "error:" in lowered) and "0 errors" not in lowered:
                errors.append(stripped[:300])

    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        # Skip tallies and the banner that merely says where warnings are written.
        if ("warning" in lowered and "0 warnings" not in lowered
                and "warnings go to" not in lowered and "warning(s) go to" not in lowered):
            warnings.append(stripped[:300])
    return errors, warnings[:20]


def _parse_number(token):
    token = token.strip().rstrip(",")
    if "," in token:  # complex pair "re,im"
        real, imag = token.split(",", 1)
        return complex(float(real), float(imag))
    return float(token)


def parse_ascii_raw(text):
    """Parse a SPICE ASCII rawfile into a list of plots.

    Format written by ngspice when ``SPICE_ASCIIRAWFILE=1`` is set: a header
    block per analysis, then a ``Values:`` section where the first line of each
    point starts at column 0 and continuation lines are indented. Only the last
    whitespace-separated token of a line is a number, so all values are
    collected in order and reshaped by the variable count -- that stays correct
    for both real and complex data without depending on indentation width.
    """
    plots = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        row = lines[index]
        if not row.startswith("Plotname:"):
            index += 1
            continue
        plot = {"name": row.split(":", 1)[1].strip(), "flags": "",
                "points": 0, "variables": [], "data": {}}
        index += 1
        while index < len(lines) and not lines[index].startswith("Variables:"):
            head = lines[index]
            if head.startswith("Flags:"):
                plot["flags"] = head.split(":", 1)[1].strip()
            elif head.startswith("No. Points:"):
                plot["points"] = int(head.split(":", 1)[1].strip())
            index += 1
        index += 1  # skip "Variables:"
        while index < len(lines) and not lines[index].startswith("Values:"):
            tokens = lines[index].split()
            if len(tokens) >= 2:
                plot["variables"].append(tokens[1])
            index += 1
        index += 1  # skip "Values:"
        flat = []
        while index < len(lines):
            row = lines[index]
            if row.startswith(("Plotname:", "Title:", "Command:", "Date:")):
                break
            if row.strip():
                try:
                    flat.append(_parse_number(row.split()[-1]))
                except ValueError:
                    pass
            index += 1
        names = plot["variables"]
        count = len(names)
        if count:
            for position, name in enumerate(names):
                plot["data"][name] = flat[position::count]
            if not plot["points"]:
                plot["points"] = len(plot["data"][names[0]])
        plots.append(plot)
    return plots


def parse_psf_ascii(text):
    """Parse scalar/vector VALUE records out of a PSF-ASCII file.

    Deliberately small: it reads ``"name" value`` scalars and
    ``"name" (v1 v2 ...)`` vectors and ignores the SECTION/TYPE layout. Anything
    it cannot understand is simply absent from the result, which stops a caller
    from treating a half-parsed file as a successful run.

    Vectors may be split over several lines (``"x" (`` then the numbers then
    ``)``); those are accumulated, because dropping them would silently lose
    waveform data.
    """
    data = {}
    pending = None          # a scalar whose value sits on the next line
    vector_name = None      # a vector currently being accumulated
    vector_values = []

    def flush_vector():
        if vector_name is not None and vector_values:
            data[vector_name] = list(vector_values)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if vector_name is not None:
            body = line.rstrip(")").strip()
            closed = line.endswith(")")
            for token in body.split():
                try:
                    vector_values.append(float(token))
                except ValueError:
                    vector_values.append(float("nan"))
            if closed:
                flush_vector()
                vector_name, vector_values = None, []
            continue

        if line.startswith(("HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END", "SECTION")):
            pending = None
            continue
        if not line.startswith('"'):
            if pending:
                try:
                    data[pending] = float(line)
                except ValueError:
                    pass
                pending = None
            continue
        closing = line.find('"', 1)
        if closing < 0:
            continue
        name = line[1:closing]
        rest = line[closing + 1:].strip()
        if rest.startswith("("):
            if rest.endswith(")"):
                values = []
                for token in rest.strip("()").split():
                    try:
                        values.append(float(token))
                    except ValueError:
                        values.append(float("nan"))
                if values:
                    data[name] = values
            else:
                # "name" (        <- values continue on the following lines
                vector_name = name
                vector_values = []
                for token in rest.lstrip("(").split():
                    try:
                        vector_values.append(float(token))
                    except ValueError:
                        vector_values.append(float("nan"))
        elif rest:
            try:
                data[name] = float(rest)
            except ValueError:
                pending = name
        else:
            pending = name
    flush_vector()
    return data


def scalar(data, key):
    """Return one exact, finite real scalar (raises instead of guessing).

    A single-point analysis (``.op``, a one-step sweep) is stored as a
    one-element vector, so that form is accepted too -- otherwise the most
    common "read the operating point" call would fail on a technicality.
    """
    if key not in data:
        raise ValueError("result has no key %r" % key)
    value = data[key]
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("key %r is a %d-element vector, not a scalar"
                             % (key, len(value)))
        value = value[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("key %r is not a real scalar" % key)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("key %r is not finite" % key)
    return number


def vector(data, key):
    """Return one non-empty finite list of numbers (complex allowed)."""
    if key not in data:
        raise ValueError("result has no key %r" % key)
    value = data[key]
    if not isinstance(value, list) or not value:
        raise ValueError("key %r is not a non-empty vector" % key)
    numbers = [complex(item) for item in value]
    if not all(math.isfinite(item.real) and math.isfinite(item.imag) for item in numbers):
        raise ValueError("key %r contains non-finite values" % key)
    return numbers


def _quote_all(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


def build_command(backend, *, netlist, workdir, log, raw, mode=None, args=None):
    """Build the shell command (and extra environment) for one backend.

    Validation happens here, so an unsupported mode fails before anything is
    executed on the target.
    """
    args = list(args or [])
    if backend == "ngspice":
        if mode and mode not in NGSPICE_MODES:
            raise SimulatorError("unsupported ngspice mode %r (expected one of %s)"
                                 % (mode, ", ".join(NGSPICE_MODES)))
        binary = config.NGSPICE_BIN or "ngspice"
        # -b batch, -o log, -r rawfile. ASCII rawfile comes from the environment
        # so we can parse results without a binary reader.
        command = _quote_all([binary, "-b", "-o", log, "-r", raw, netlist] + args)
        return command, {"SPICE_ASCIIRAWFILE": "1"}
    if backend == "spectre":
        if mode and mode not in SPECTRE_MODES:
            raise SimulatorError("unsupported Spectre mode %r (expected one of %s)"
                                 % (mode, ", ".join(sorted(SPECTRE_MODES))))
        argv = shlex.split(config.SPECTRE_BIN or "spectre")
        argv.extend(["-64", netlist, "+escchars"])
        if log:
            argv.extend(["+log", log])
        argv.extend(["-format", "psfascii"])
        if raw:
            argv.extend(["-raw", raw])
        argv.extend(SPECTRE_MODES[mode] if mode else [])
        argv.extend(["+lqtimeout", "900", "-maxw", "5", "-maxn", "5", "+logstatus"])
        argv.extend(args)
        return _quote_all(argv), {}
    if backend == "custom":
        if not config.SIM_CMD:
            raise SimulatorError(
                "backend 'custom' needs PYAETHER_SIM_CMD, for example "
                "\"mysim -b {netlist} -o {log}\"")
        # Quote every substituted value: run directories and user paths may
        # contain spaces, which would otherwise split into several arguments.
        command = config.SIM_CMD.format(
            netlist=shlex.quote(netlist), workdir=shlex.quote(workdir),
            log=shlex.quote(log), raw=shlex.quote(raw),
            mode=shlex.quote(mode or ""))
        if args:
            command += " " + _quote_all(args)
        return command, {}
    raise SimulatorError("unknown backend %r (expected %s)"
                         % (backend, ", ".join(sorted(backends()))))


def resolve_target(backend=None):
    """Choose where a backend runs; returns ``(kind, reason)``.

    ``PYAETHER_SIM_TARGET`` always wins when it is set. Otherwise the choice
    follows the nature of the tool:

      * open-source backends (ngspice) prefer **this machine** when the binary
        is installed here, because they need no licence and are not part of the
        EDA installation -- the Aether container usually has no ngspice at all;
      * commercial backends (Spectre) and ``custom`` templates keep using the
        bridge target, because they live next to the licensed EDA environment.
    """
    if config.SIM_TARGET:
        return config.SIM_TARGET.lower(), "PYAETHER_SIM_TARGET is set"
    if (backend or config.SIM_BACKEND or "").lower() == "ngspice":
        local_binary = shutil.which(config.NGSPICE_BIN or "ngspice")
        if local_binary:
            return "local", "ngspice found on this machine at %s" % local_binary
        return (config.TRANSPORT or "local").lower(), \
            "no local ngspice; falling back to the bridge target"
    return (config.TRANSPORT or "local").lower(), "commercial/custom backend follows the bridge target"


def transport_for_simulator(backend=None):
    """Build the transport selected by :func:`resolve_target`."""
    kind, _reason = resolve_target(backend)
    if not config.SIM_TARGET and kind == (config.TRANSPORT or "").lower():
        return transports.build()
    if kind == "docker":
        return transports.DockerTransport(
            config.SIM_CONTAINER or config.CONTAINER,
            config.SIM_WORKDIR, config.PYTHON, license_server=config.LICENSE_SERVER)
    if kind == "ssh":
        host = config.SIM_SSH_HOST or config.SSH_HOST
        if not host:
            raise SimulatorError("simulator target 'ssh' needs PYAETHER_SIM_SSH_HOST "
                                 "(or PYAETHER_SSH_HOST)")
        return transports.SSHTransport(host, config.SIM_WORKDIR, config.PYTHON,
                                       port=config.SIM_SSH_PORT or config.SSH_PORT,
                                       options=config.SIM_SSH_OPTS or config.SSH_OPTS)
    if kind == "local":
        return transports.LocalTransport(config.SIM_WORKDIR, config.PYTHON)
    raise SimulatorError("unknown simulator target %r (expected docker / ssh / local)" % kind)


def probe(backend, *, transport=None, timeout=60.0):
    """Check whether a backend is usable on the target (binary found + version)."""
    if backend not in backends():
        raise SimulatorError("unknown backend %r (expected %s)"
                             % (backend, ", ".join(sorted(backends()))))
    target = transport or transport_for_simulator(backend)
    if backend == "custom":
        configured = bool(config.SIM_CMD)
        return {"backend": backend, "target": target.label(), "available": configured,
                "detail": "" if configured else "PYAETHER_SIM_CMD is not set",
                "version": "", "command": config.SIM_CMD or ""}
    binary = (config.NGSPICE_BIN if backend == "ngspice" else config.SPECTRE_BIN) or backend
    command = "command -v %s && (%s -v 2>&1 | head -3 || true)" % (
        shlex.quote(binary), shlex.quote(binary))
    try:
        result = target.run(command, timeout=timeout)
    except transports.TransportError as exc:
        return {"backend": backend, "target": target.label(), "available": False,
                "detail": str(exc), "version": "", "command": binary}
    available = result["rc"] == 0 and bool(result["stdout"])
    version = ""
    if available:
        for line in (result["stdout"] or "").splitlines():
            if not line.startswith("/"):
                version = line.strip()
                break
    return {"backend": backend, "target": target.label(), "available": available,
            "detail": "" if available else ("%s not found on target" % binary),
            "version": version, "command": binary}


def _stage(target, workdir, netlist_path, netlist_text, includes):
    target.run("mkdir -p %s" % shlex.quote(workdir))
    target.write_file(netlist_path, netlist_text.encode("utf-8"))
    staged = []
    for include in includes or []:
        destination = os.path.join(workdir, os.path.basename(include))
        try:
            with open(include, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            raise SimulatorError("cannot read include file %s: %s" % (include, exc))
        target.write_file(destination, payload)
        staged.append(destination)
    return staged


def _fetch(target, remote, into):
    local = os.path.join(into, os.path.basename(remote))
    try:
        target.fetch_file(remote, local)
        return local
    except Exception:
        return ""


def _read(path):
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def run(netlist, *, backend=None, transport=None, workdir=None, timeout=None,
        mode=None, args=None, includes=None, netlist_text=None, run_id=None):
    """Run one netlist and return a result dict (``ok``/``status``/``data``/...).

    ``netlist``      path of the netlist; when ``netlist_text`` is given the
                     text is staged instead of reading that path locally.
    ``backend``      ngspice / spectre / custom (default ``PYAETHER_SIM_BACKEND``).
    ``transport``    override the target; defaults to ``transport_for_simulator()``.
    """
    backend = (backend or config.SIM_BACKEND or "ngspice").lower()
    if backend not in backends():
        raise SimulatorError("unknown backend %r (expected %s)"
                             % (backend, ", ".join(sorted(backends()))))
    target = transport or transport_for_simulator(backend)
    timeout = float(timeout or config.SIM_TIMEOUT or 600)
    # A second-resolution id collides when two runs start in the same second,
    # and two runs sharing a directory overwrite each other's netlist, log and
    # rawfile. Add a short random suffix so the run directory is unique.
    run_id = run_id or "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    run_dir = os.path.join(workdir or config.SIM_WORKDIR,
                           "run-%s-%s" % (backend, run_id))

    if netlist_text is None:
        try:
            with open(netlist, encoding="utf-8", errors="replace") as handle:
                netlist_text = handle.read()
        except OSError as exc:
            raise SimulatorError("cannot read netlist %s: %s" % (netlist, exc))
    netlist_path = os.path.join(run_dir, os.path.basename(netlist))
    log_path = os.path.join(run_dir, "sim.log")
    raw_path = os.path.join(run_dir, "raw.out")

    started = time.time()

    def metadata(extra=None):
        info = {
            "target": target.label(),
            "target_reason": resolve_target(backend)[1],
            "work_dir": run_dir,
            "netlist": netlist_path,
            "timings": {"total_s": round(time.time() - started, 3)},
        }
        if extra:
            info.update(extra)
        return info

    staging_started = time.time()
    staged_includes = _stage(target, run_dir, netlist_path, netlist_text, includes)
    staging_seconds = round(time.time() - staging_started, 3)

    command, env = build_command(backend, netlist=netlist_path, workdir=run_dir,
                                 log=log_path, raw=raw_path, mode=mode, args=args)
    try:
        executed = target.run(command, timeout=timeout, env=env, run_dir=run_dir)
    except transports.TransportTimeout as exc:
        # Killing the local client does not stop the simulator: `docker exec`
        # and `ssh` leave the remote process running (measured -- a 3 s timeout
        # left `sleep 45` alive inside the container, which in a real run means
        # a licence seat and CPU cores stay busy). Ask the target to clean up by
        # the run directory, which is unique per run.
        cleanup = target.cleanup(run_dir)
        return _result(STATUS_FAILURE, backend,
                       errors=["timed out after %.0fs: %s" % (timeout, exc),
                               "cleanup: %s" % cleanup],
                       metadata=metadata({"command": command, "includes": staged_includes,
                                          "timed_out": True,
                                          "timings": {"staging_s": staging_seconds,
                                                      "total_s": round(time.time() - started, 3)}}))
    except transports.TransportError as exc:
        return _result(STATUS_FAILURE, backend,
                       errors=["transport failure: %s" % exc],
                       metadata=metadata({"command": command, "includes": staged_includes,
                                          "timings": {"staging_s": staging_seconds,
                                                      "total_s": round(time.time() - started, 3)}}))

    execution_seconds = round(time.time() - started - staging_seconds, 3)
    errors, warnings = _classify((executed["stdout"] or "") + "\n" + (executed["stderr"] or ""))

    parse_started = time.time()
    listing = target.run("ls -1 %s 2>/dev/null" % shlex.quote(run_dir), timeout=60.0)
    artifacts = [name.strip() for name in (listing["stdout"] or "").splitlines() if name.strip()]
    local_root = tempfile.mkdtemp(prefix="pyaether-sim-")
    local_log = _fetch(target, log_path, local_root)
    local_raw = _fetch(target, raw_path, local_root)

    log_text = _read(local_log)
    if log_text:
        extra_errors, extra_warnings = _classify(log_text)
        errors.extend(item for item in extra_errors if item not in errors)
        warnings.extend(item for item in extra_warnings if item not in warnings)

    raw_text = _read(local_raw)
    data = {}
    plots = []
    if raw_text.lstrip().startswith(("Title:", "Plotname:")):
        plots = parse_ascii_raw(raw_text)
        for plot in plots:
            for name, values in plot["data"].items():
                if values:
                    data[name] = values
    elif raw_text:
        data = parse_psf_ascii(raw_text)

    if executed["rc"] != 0:
        status = STATUS_FAILURE
        errors.append("simulator exited with rc=%s" % executed["rc"])
    elif errors:
        status = STATUS_FAILURE
    elif not data:
        # Clean exit but nothing we could understand: do not claim success
        # without usable numbers.
        status = STATUS_PARTIAL
        errors.append("no parseable result data (check the raw output format)")
    else:
        status = STATUS_SUCCESS

    return _result(
        status, backend,
        data=data, errors=errors, warnings=warnings,
        metadata=metadata({
            "command": command,
            "includes": staged_includes,
            "mode": mode or "",
            "returncode": executed["rc"],
            "artifacts": artifacts,
            "plots": [{"name": plot["name"], "points": plot["points"],
                       "variables": plot["variables"]} for plot in plots],
            "stdout_tail": (executed["stdout"] or "")[-2000:],
            "stderr_tail": (executed["stderr"] or "")[-2000:],
            "timings": {"staging_s": staging_seconds, "execution_s": execution_seconds,
                        "parse_s": round(time.time() - parse_started, 3),
                        "total_s": round(time.time() - started, 3)},
        }),
    )
