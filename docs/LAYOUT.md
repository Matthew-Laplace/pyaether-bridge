# Layout with KLayout

The bridge generates, reads, checks and converts layouts through **KLayout**,
driven the way its authors support headless use:

```
klayout -b -r <script>
```

`-b` is KLayout's batch mode, and it is exactly `-zz -nc -rx`: no GUI, no
configuration file read or written, no implicit macros. That matters for us --
it means several runs can execute in parallel without fighting over the user's
KLayout configuration, and no display or licence is needed.

## Why this shape

KLayout ships its own Python interpreter and the `pya` module *inside* the
application. There is no supported way to `import pya` from a normal Python
installation, and the `klayout` package on PyPI is a different, UI-less subset.
So the bridge does not import anything: it writes a small script, runs KLayout
on it, and reads back a JSON report. That keeps this project on the Python
standard library and keeps KLayout a separate, user-installed tool.

**Licence boundary.** KLayout and its `pya` bindings are GPL-3.0. This bridge
never imports or links them; it invokes the `klayout` executable as a separate
process and exchanges files and JSON. The script it writes is our own MIT code
that merely runs on top of KLayout. Do not add `import klayout.db` or `import pya`
to the host-side code: that would pull the GPL component into this process.

## Operations

| Operation | What it does |
| --- | --- |
| `gen` | build GDS2 or OASIS from a JSON spec: boxes, polygons, paths, texts, cells, instance arrays |
| `info` | read any layout: top cells, per-layer shape counts, instance counts, extents |
| `drc` | geometric checks: `width`, `space`, `notch`, `enclosing`, `area` |
| `boolean` | layer algebra: `merge`, `and`, `not`, `xor`, `size`, `grow`, `shrink` |
| `convert` | reformat or clip with KLayout's stream tools (`strm2oas`, `strm2gds`, `strmclip`, ...) |
| `compare` | compare two layouts (`strmcmp`) or XOR them (`strmxor`) |
| `deck` | run a KLayout rule deck (`.drc` / `.lvs`) through the real engine |

## CLI

```bash
./bin/pyaether layout probe                                  # is KLayout available, which version
./bin/pyaether layout gen spec.json -o out.gds               # build
./bin/pyaether layout info out.gds --layers '{"m1":[1,0]}'   # read back
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout boolean and --a 1/0 --b 2/0 --out-layer 4/0 --source out.gds -o and.gds
./bin/pyaether layout tools                                  # the stream tools on this target
./bin/pyaether layout convert out.gds out.oas                # format change
./bin/pyaether layout compare a.gds b.gds                    # exit 0 = identical
./bin/pyaether layout deck rules.drc --source out.gds
```

Exit codes: `0` success/clean; `1` could not run (missing tool, unresolvable
layer, bad spec); **`3` the DRC ran and found violations** -- a dirty layout must
not look like success in CI. `--exit-zero` converts that back to 0 for scripts
that want to inspect the result themselves. `layout compare` exits `0` when the
layouts are identical and `1` when they differ, mirroring `strmcmp`.

## MCP

`pyaether_layout_gen`, `pyaether_layout_info`, `pyaether_layout_drc`,
`pyaether_layout_boolean`, `pyaether_layout_convert`, `pyaether_layout_compare`,
`pyaether_layout_deck`.

## The spec

```json
{
  "top": "TOP",
  "dbu": 0.001,
  "layers": {"m1": [1, 0], "m2": [2, 0], "contact": [3, 0]},
  "cells": {
    "UNIT": {"shapes": [{"layer": "m1", "box": [0, 0, 2, 1]}]},
    "TOP": {
      "shapes": [
        {"layer": "m2", "box": [0, 0, 12, 8]},
        {"layer": "m1", "polygon": [[0, 0], [3, 0], [1.5, 2]]},
        {"layer": "m1", "path": [[0, 6], [10, 6]], "width": 0.4},
        {"layer": "m1", "text": "T1", "at": [1, 7]}
      ],
      "instances": [
        {"cell": "UNIT", "at": [1, 1], "columns": 3, "rows": 2, "dx": 3, "dy": 3}
      ]
    }
  }
}
```

Lengths are in microns (`unit` selects otherwise). `instances` may use
`rotation`, `mirror`, `columns`/`rows` with `dx`/`dy` (and `dx_row`/`dy_row`).

## Rules

```json
[
  {"name": "m1 width", "check": "width", "layer": "m1", "value": 0.2},
  {"name": "m2 encloses m1", "check": "enclosing", "layer": "m2", "other": "m1", "value": 0.1},
  {"name": "contact min area", "check": "area", "layer": "contact", "value": 0.05}
]
```

A layer is a name (resolved through `layers` or the file's own layer names) or a
`"layer/datatype"` string. Two properties of the implementation are deliberate:

* **Touching edges are not violations.** Merged geometry produces edge pairs at
  distance 0 where polygon boundaries meet. Counting those as spacing errors
  makes any real layout look broken, so they are removed and the number removed
  is reported as `touching_edges_ignored`.
* **A rule that cannot be evaluated fails the run.** An unknown layer or an
  unsupported check produces an error, never "0 violations". A check that did not
  run tells you nothing about the layout.

## Layer names: GDS2 vs OASIS

GDS2 has no layer-name table; OASIS does. Passing `--layers` is therefore
required when you address layers by name in a `.gds` -- without it the run fails
with "unknown layer" rather than silently checking nothing.

## What this does not do

* **No foundry rules.** KLayout ships no rule decks; `drc` runs the checks you
  list, and `deck` runs a deck you supply. Neither is a signoff.
* **No LVS verdict.** A `.lvs` deck can be executed, but comparing against a
  vendor netlist belongs in the schematic/EDA flow, not here.
* **`deck` reports what it can read.** On KLayout 0.30.10 for macOS the engine
  executes a `.drc` deck (the DSL calls are traced) but does not write the report
  database in batch mode. A run with no report comes back as `PARTIAL` with that
  explanation -- never as "clean".
* **Stream tools are not on `PATH`.** They live in the application bundle (for
  example `KLayout.app/Contents/Buddy`); the bridge finds them next to the
  `klayout` binary or via `PYAETHER_KLAYOUT_BUDDY_DIR`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `PYAETHER_KLAYOUT_BIN` | KLayout executable (default `klayout`) |
| `PYAETHER_KLAYOUT_TARGET` | Where to run it: `docker` / `ssh` / `local` (default: this machine when `klayout` is installed here, else the bridge target) |
| `PYAETHER_KLAYOUT_BUDDY_DIR` | Directory holding the stream tools (default: inferred from the KLayout binary) |
| `PYAETHER_LAYOUT_WORKDIR` | Run directory root on the target (default `/tmp/pyaether-layout`) |
| `PYAETHER_LAYOUT_TIMEOUT` | Default timeout in seconds (default 600) |
