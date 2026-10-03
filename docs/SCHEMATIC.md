# Schematic round trip (canvas <-> Aether)

Draw in a canvas, get a real schematic in Aether -- and read an Aether
schematic back out as a snapshot a canvas can open.

```bash
# canvas/Aether -> snapshot (reads a live schematic)
./bin/pyaether sch snapshot myLib myCell -o mycell.json

# canvas document or snapshot -> a real schematic in Aether
./bin/pyaether sch build mycell.json --library scratch --cell mycell_copy

# SPICE text from the same connectivity (no session needed)
./bin/pyaether sch netlist mycell.json -o mycell.sp

# build, read back, and compare the wiring
./bin/pyaether sch roundtrip mycell.json
```

MCP tools: `pyaether_sch_build`, `pyaether_sch_netlist`.

## Interchange format

`analog-agent.schematic` (schemaVersion 1) -- the same snapshot the existing
Aether-to-canvas importers use, so both directions interoperate:

```json
{"format": "analog-agent.schematic", "schemaVersion": 1,
 "source": {"library": "myLib", "cell": "myCell", "view": "schematic"},
 "instances": [{"id": "i1", "name": "X1", "library": "analog", "cell": "res",
                "view": "symbol", "position": [100, 200],
                "terminals": [{"name": "PLUS", "netId": "IN"}]}],
 "nets": [{"id": "IN", "name": "IN", "isGlobal": false,
           "terminals": [{"instanceId": "i1", "pinName": "PLUS"}]}],
 "terminals": [{"name": "IN", "netId": "IN", "direction": "input"}]}
```

An **Analog Canvas document** is accepted directly: the same facts are read from
`reference`, `placement.position`, `connectivityEvidence[].name`,
`netlist.terminals[].id`, and `nets[].terminals[].{instanceId,pinName}`.

## Behaviours worth knowing

* **The target cell must be known.** A brand-new canvas drawing has no Aether
  binding, so `library`/`cell` come from the command line (or `sourceBinding`).
  Without them the run is refused -- it does not produce a half-empty sheet.
* **A missing library is created.** If the target library does not exist it is
  created with `dbCreateLib`, so a fresh drawing can land in a new library.
* **Positions.** A spec with real coordinates keeps them; a spec whose instances
  are all at (0, 0) is spread on a grid so the result is not a pile of symbols.
* **An existing cell is not overwritten.** Re-running into the same cell reports
  "instance ... already exists" per instance rather than duplicating or wiping
  the design. Use a new cell name (or delete the view first).
* **A netlist is a structural netlist.** Devices and parameters come from the
  drawing, not from a PDK, and the header states whether the file is a positional
  SPICE deck (pin order known, e.g. read back from Aether) or a connectivity edge
  list (pin order unknown, so each connection is spelled out instead of being
  silently shuffled).
* **Pin names must exist on the master symbol.** A pin name the symbol does not
  have creates no connection; the build reports it as a problem.

## Verified

`tests/schematic_probe.py`:

* offline (always): a canvas document normalizes to the snapshot shape, the
  formal interface is picked up, an unbound drawing is refused, an instance
  without a master is reported, and the SPICE writer emits every connection;
* live (opt-in): a real 17-instance design was read, rebuilt into another cell
  and read back -- 17 instances, 10 nets, 22 connections, 10 pins, **zero
  missing and zero invented connections**.

```bash
PYAETHER_SCH_PROBE_SOURCE_LIB=TOPO_05 PYAETHER_SCH_PROBE_SOURCE_CELL=test \
PYAETHER_SCH_PROBE_LIB=mywork PYAETHER_SCH_LIBDEFS=/path/to/lib.defs \
python3 tests/schematic_probe.py
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `PYAETHER_SCH_WORKDIR` | Run directory root on the target (default `/tmp/pyaether-sch`) |
| `PYAETHER_SCH_LIBDEFS` | Directory whose `lib.defs` registers the libraries (a file path is reduced to its directory). Point it at `$AETHER_HOME/lib` for the vendored libraries. |
| `PYAETHER_SCH_AETHER_ROOT` | Alternative to `PYAETHER_SCH_LIBDEFS`: the Aether root; `lib/` is used |

Two measured gotchas are encoded in the module:

* `dbSwitchLibDefsPath` takes a **directory** (it looks for `lib.defs` inside);
  passing the file fails with `NotADirectoryError`.
* A `lib.defs` entry with a bare name (`DEFINE myLib myLib`) resolves next to the
  file and works; pointing `DEFINE` at an absolute path outside that directory
  did not register the library in testing, so a library layout that mixes both
  is best expressed with the definitions kept together.
