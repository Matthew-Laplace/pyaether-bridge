# Simulators

The bridge can run a netlist on different simulators without changing the
netlist workflow, the CLI surface, or the result format:

| Backend | Kind | Binary | Modes |
| --- | --- | --- | --- |
| `ngspice` | open source | `PYAETHER_NGSPICE_BIN` (default `ngspice`) | analysis declared in the netlist (`tran`/`ac`/`dc`/`op`/`noise`) |
| `spectre` | commercial | `PYAETHER_SPECTRE_BIN` (default `spectre`) | `spectre`, `aps`, `x`, `cx`, `ax`, `mx`, `lx`, `vx` |
| `alps` | commercial (Empyrean) | `PYAETHER_ALPS_BIN` (default `alps`) | `basic`, `turbo`, `pro` (accuracy levels) |
| `custom` | any | command template in `PYAETHER_SIM_CMD` | whatever your tool takes |

The point is that **the simulator does not have to be the one that ships with
your schematic tool**. A netlist can be checked with open-source ngspice on a
laptop and signed off with Spectre/APS on a licensed server, and both runs come
back through the same result contract.

## Where simulators run

`PYAETHER_SIM_TARGET` selects the machine, independently of the PyAether target
(`PYAETHER_TRANSPORT`). When you do not set it, the choice follows the nature of
the tool rather than blindly reusing the PyAether target:

* **open-source backends (ngspice) prefer this machine** when the binary is
  installed here -- they need no licence and are not part of the EDA
  installation, and the Aether container usually has no ngspice at all;
* **`spectre` and `custom` keep the bridge target**, because they live next to
  the licensed EDA environment.

Every result records which rule applied in `metadata.target_reason`.

```bash
# ngspice on this laptop, PyAether in a container -- this is the default
# behaviour, but you can also pin it explicitly:
export PYAETHER_TRANSPORT=docker
export PYAETHER_SIM_TARGET=local

# Spectre on the licensed server, PyAether also there
export PYAETHER_TRANSPORT=ssh
export PYAETHER_SIM_TARGET=ssh
export PYAETHER_SIM_SSH_HOST=aether@lab-server
```

The simulator layer reuses the transport implementations, so `docker`, `ssh` and
`local` behave exactly as described in the main README (login shell, no stored
credentials, `BatchMode` for SSH).

## CLI

```bash
# what is available, and where
./bin/pyaether sim backends --probe

# open-source run
./bin/pyaether sim run rc.cir --backend ngspice

# Cadence run with the APS extended engine
./bin/pyaether sim run tb.scs --backend spectre --mode ax

# stage model / Verilog-A files next to the netlist
./bin/pyaether sim run tb.scs --backend spectre --include models.lib --include adc.va

# machine-readable result
./bin/pyaether sim run rc.cir --backend ngspice --json
```

Exit code is `0` only when `result["ok"]` is true.

## MCP

```jsonc
{"name": "pyaether_sim_run",
 "arguments": {"netlist": "* rc\nV1 in 0 ac 1\nR1 in 0 1k\nC1 in 0 1u\n.ac dec 10 1 1meg\n.end\n",
               "backend": "ngspice",
               "timeout": 120}}
```

## Result contract

Every run returns the same shape, whatever the backend:

```json
{
  "ok": true,
  "status": "SUCCESS",
  "backend": "ngspice",
  "data": {"frequency": ["..."], "v(out)": ["..."]},
  "errors": [],
  "warnings": [],
  "metadata": {
    "target": "local",
    "command": "ngspice -b -o .../sim.log -r .../raw.out .../rc.cir",
    "work_dir": "/tmp/pyaether-sim/run-ngspice-20261003-135942",
    "mode": "",
    "returncode": 0,
    "artifacts": ["rc.cir", "raw.out", "sim.log"],
    "plots": [{"name": "AC Analysis", "points": 41,
               "variables": ["frequency", "v(in)", "v(out)", "i(v1)"]}],
    "timings": {"staging_s": 0.009, "execution_s": 0.026,
                "parse_s": 0.01, "total_s": 0.045}
  }
}
```

Three rules make the result trustworthy:

1. **`ok` is the execution contract.** A non-empty `data` is not proof of
   success: a crashed run can leave partial raw files behind. `status` is
   `SUCCESS`, `PARTIAL` (ran, but no parseable numbers) or `FAILURE`.
2. **Failures are classified**, not dumped: `netlist read error`, `license
   error`, `convergence failure`, `file not found`, `simulator crashed`, plus
   the raw exit code.
3. **No invented numbers.** Parsers only report what they understood; use the
   strict accessors below when a downstream calculation must fail loudly.
4. **A timeout cleans up after itself.** Killing the local `docker exec`/`ssh`
   client does *not* stop the simulator on the far side, so every run is started
   in its own process group and, on timeout, that group is killed and the result
   reports what the cleanup found. Without this a timed-out run keeps burning a
   licence seat and CPU cores.

```python
from pyaether_bridge import simulators

result = simulators.run("rc.cir", backend="ngspice")
if not result["ok"]:
    raise SystemExit(result["errors"])
freq = simulators.vector(result["data"], "frequency")   # non-empty, finite
gain = simulators.scalar(result["data"], "dc_gain")     # exact real scalar
```

`vector()` and `scalar()` raise `ValueError` on a missing key, an empty vector,
a non-numeric or non-finite value, so a wrong signal name cannot silently
become a number.

## Adding another open-source simulator

Two options, both without forking the bridge:

**1. Reuse the `custom` backend.** Give the command template; `{netlist}`,
`{workdir}`, `{log}`, `{raw}` and `{mode}` are substituted (each value is
shell-quoted, so paths with spaces are safe):

```bash
# Xyce reading a SPICE deck
export PYAETHER_SIM_CMD='Xyce -l {log} -r {raw}.raw {netlist}'
./bin/pyaether sim run rc.cir --backend custom

# an in-house simulator, or the vendor simulator that ships with your schematic tool
export PYAETHER_SIM_CMD='ae-sim -batch -i {netlist} -o {workdir}'
```

**Ask the tool for a text rawfile.** The parser reads ASCII; a binary rawfile
leaves `data` empty and the run comes back as `PARTIAL` (the bridge will not
pretend it read numbers it did not). Set `PYAETHER_SIM_ENV` — it applies to
every backend and overrides built-in defaults:

```bash
export PYAETHER_SIM_CMD='ngspice -b -o {log} -r {raw} {netlist}'
export PYAETHER_SIM_ENV='SPICE_ASCIIRAWFILE=1'
./bin/pyaether sim run rc.cir --backend custom
```

When the rawfile turns out to be binary the error says so explicitly and names
this switch, instead of blaming the output format. Either way the run still
returns `ok`/`errors`/`artifacts`, so a tool whose output cannot be parsed is
reported honestly rather than silently.

**3. Vendor flow (Empyrean ALPS).** The `alps` backend drives the simulator that
Aether/MDE normally configures in the GUI, through its own batch CLI:

```bash
./bin/pyaether sim backends --probe                       # is alps on the target?
./bin/pyaether sim run amp.sp --backend alps --mode turbo
```

Command shape: `alps -o <run>/psf -log sim.log [-mode basic|turbo|pro] [-mt N]
<netlist>` (options and accuracy levels taken from `alps -h`). ALPS writes its
results as PSF below `-o`; this parser reads the small text ones and otherwise
reports `PARTIAL` with the artifact list, because the PSF layout could not be
verified on this machine (see below). **ALPS needs a valid Empyrean licence.**
The bridge only invokes the tool you installed and never reads, alters or works
around licensing.

**2. Add a first-class backend** in `pyaether_bridge/simulators.py`:

* add an entry to `backends()` (name, kind, binary env var, modes, notes)
* build the command in `build_command()`
* parse the output in `run()` where `parse_ascii_raw` / `parse_psf_ascii` are
  dispatched
* teach `probe()` how to check the binary

A backend is only worth adding when its flags are known from documentation you
can cite -- the bridge deliberately refuses to guess flags for an unknown tool.

## Verified behaviour

`tests/simulator_probe.py` runs a real ngspice simulation of an RC low-pass and
checks the numbers against theory (DC gain 1, -3 dB at `1/(2*pi*R*C)` =
159.15 Hz, steeper than -15 dB roll-off), plus failure classification, Spectre
mode construction, the `custom` backend driving a real tool, target resolution
and the strict accessors. It needs no Spectre licence and no network.

`tests/alps_live_probe.py` (opt-in: `PYAETHER_ALPS_PROBE=1`) drives the vendor
simulator through the bridge. On a machine where the vendor binary runs it
verifies a full run; where it cannot run, the probe checks that the bridge
reports the failure honestly and attributes it correctly (licence vs. host)
instead of blaming the bridge or faking success.

Not covered:

* a live Spectre run -- the mode flags and command shape are built and
  unit-checked, but no Spectre licence was available;
* a completed ALPS run -- on this host the vendor binary aborts because it runs
  under x86_64 emulation (`Exe = /run/rosetta/rosetta` in its log, no licence
  error present), so the engine itself is untested here;
* PSF parsing beyond scalar and vector `VALUE` records.
