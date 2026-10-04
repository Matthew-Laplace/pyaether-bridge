---
name: pyaether-bridge
description: "Drive Empyrean Aether/PyAether, SPICE simulators (ngspice, Spectre/APS, Empyrean ALPS), schematic round trips (canvas drawing <-> Aether schematic) and KLayout layouts through the pyaether-bridge MCP tools. Use when a task needs to run Python inside a live pyAether session, look up a PyAether API signature, run a netlist on a chosen simulator, turn a canvas drawing into an Aether schematic (or read one back), or generate/inspect/check/convert/compare a GDS or OASIS layout."
whenToUse: "The task involves Aether/PyAether automation, SPICE simulation from a netlist, schematic round trip between a canvas and Aether, or KLayout layout generation and design-rule checks, and the pyaether MCP tools (mcp__pyaether__*) are available."
---

# pyaether-bridge

The bridge keeps one pyAether session alive on a target (a container, a server
over SSH, or this machine) and exposes it, plus a simulator layer, a schematic
round-trip layer and a KLayout layout layer, as MCP tools. Tools arrive as
`mcp__pyaether__<name>`.

## Tools

| Tool | Use it for |
| --- | --- |
| `pyaether_status` | Is the daemon up, is the target reachable, is a session ready. Read-only; never starts anything. |
| `pyaether_api_search` | Find a PyAether API by name or keyword before calling it. |
| `pyaether_api_help` | Read one symbol's exact signature, parameters and return value. |
| `pyaether_exec` | Run Python inside the resident pyAether session (variables persist between calls). |
| `pyaether_sim_run` | Run a netlist on ngspice / Spectre (incl. APS presets) / ALPS / a custom command. |
| `pyaether_layout_gen` | Build a GDS2/OASIS layout from a JSON spec. |
| `pyaether_layout_info` | Read a layout: cells, layers, shape counts, instances, extents. |
| `pyaether_layout_drc` | Run width / space / notch / enclosing / area checks. |
| `pyaether_layout_boolean` | Layer algebra: merge, and, not, xor, size. |
| `pyaether_layout_convert` | Convert or clip a layout with KLayout's stream tools (the output suffix picks the format). |
| `pyaether_layout_compare` | Compare (`strmcmp`) or XOR (`strmxor`) two layouts; `data.identical` is the answer, not the log text. |
| `pyaether_layout_deck` | Run a KLayout rule deck (`.drc` / `.lvs`) through the real engine. |
| `pyaether_sch_build` | Turn a canvas drawing or snapshot into a real Aether schematic. |
| `pyaether_sch_netlist` | Emit SPICE text from a snapshot's connectivity (works without a live session). |

## Rules that keep results honest

1. **`ok` is the execution contract.** A non-empty result body is not proof of
   success: read `ok`/`status` first, then `errors`. `status` is `SUCCESS`,
   `PARTIAL` (ran, but nothing usable came back) or `FAILURE`.
2. **Look up before calling.** Use `pyaether_api_search` / `pyaether_api_help`
   instead of guessing a PyAether API name or argument order.
3. **A DRC run with violations is not a failure of the check.** Check `clean`
   and the per-rule counts; a rule that could not be evaluated appears as
   `error` on that rule and fails the whole run.
4. **Layers by name need a map.** GDS2 stores no layer names; OASIS does. When
   reading a `.gds`, pass `layers` (for example `{"m1": [1, 0]}`). A name that
   cannot be resolved is an error, never "0 violations".
5. **Long runs take time.** Simulations and layout jobs can run for minutes;
   pass an explicit `timeout` rather than relying on the client default.
6. **Targets are independent.** PyAether, the simulator and KLayout may each run
   somewhere different (container / server / this machine). If a tool says the
   binary is missing, check which target it resolved before installing anything.
7. **A deck that runs but writes no report is `PARTIAL`.** That is not "design
   rule clean": read the deck's exit code and report database before you report
   success.

## Typical sequences

**Inspect PyAether, then act on it**

1. `pyaether_status` — confirm the target and that a session is ready.
2. `pyaether_api_search` with a keyword, then `pyaether_api_help` on the hit.
3. `pyaether_exec` with short, verifiable code; read the returned stdout.

**Run a simulation**

1. `pyaether_sim_run` with the netlist text and a `backend` (`ngspice` is the
   open-source default; `spectre` takes `mode` such as `ax` or `aps`;
   `alps` takes `basic`/`turbo`/`pro`).
2. On failure read `errors` — they are classified (netlist read error, licence
   error, convergence failure, missing file, crash) instead of raw log text.

**Generate and check a layout**

1. `pyaether_layout_gen` with a spec:
   `{top, dbu, layers: {m1: [1,0]}, cells: {TOP: {shapes: [{layer: m1, box: [0,0,10,5]}], instances: [...]}}}`
   Lengths are microns.
2. `pyaether_layout_info` to confirm what was written (shape counts, extents).
3. `pyaether_layout_drc` with rules such as
   `{name: "m1 width", check: "width", layer: "m1", value: 0.2}`.
4. `pyaether_layout_convert` to hand a different format to the next tool;
   `pyaether_layout_compare` against a golden file (`data.identical`);
   `pyaether_layout_deck` when the real rule deck, not the built-in checks, is
   the authority.

**Draw a schematic in a canvas, then put it in Aether**

1. `pyaether_sch_netlist` on the snapshot first — connectivity errors show up
   offline, before anything is written into a library.
2. `pyaether_sch_build` with the snapshot or canvas document; it creates a
   missing library and never overwrites an existing cell.
3. Read `ok` and the per-instance "already exists" notes: a cell that was
   already present is reported, not silently replaced.

## Failure handling

- A missing binary (`ngspice`, `spectre`, `alps`, `klayout`) is reported as
  "not found on target" with the resolved target name. Do not install anything
  to work around it without asking; report which target needs the tool.
- A licence error is an environment problem, not a bridge bug. The bridge never
  reads, alters or works around licensing.
- A timed-out run is cleaned up on the target; the error names the run
  directory. Check what survived before retrying.
