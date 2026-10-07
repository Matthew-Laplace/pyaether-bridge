---
name: pyaether-bridge
description: Drive Empyrean Aether/PyAether from the shell through bin/pyaether — API lookup, Python in a resident session, SPICE runs, KLayout layout operations, schematic round trip. Use for PyAether automation, netlist simulation, GDS/OASIS work, or canvas<->schematic conversion.
whenToUse: The task involves Aether/PyAether automation, SPICE simulation from a netlist, schematic round trip, or KLayout layout work, and this repository's bin/pyaether is available.
---

# pyaether-bridge (CLI)

Drive PyAether through the bridge's **command line**. The MCP server
(`bin/pyaether-mcp`) still exists and is still the supported registration route
(docs/DEEPSEEK-HARNESS.md), but an MCP registration is profile-wide: every session
in that profile pays its tool schema up front, while the CLI costs nothing until
it is used. Use the CLI unless you specifically want tool-shaped access.

```bash
PYA=./bin/pyaether               # from the repository root
"$PYA" status --json             # pass --json and read the fields, not the prose
```

## Execution contract

Read `ok` / `status` from the JSON — a non-empty body is not proof of success:

* `status`: `SUCCESS` | `PARTIAL` (ran, but nothing usable came back) | `FAILURE`
* check `ok` first, then `errors`; `exec` reports the user code's exception as
  `ok=false` with `error_type`, not as a shell failure
* human-readable output goes to stdout; diagnostics and the daemon protocol stay
  off it, so `--json` output is always parseable
* `--json` prints one compact line. Execution diagnostics (`metadata.target`,
  `target_reason`, `work_dir`, `command`, `timings`, ...) are dropped on success
  and kept on failure; `session.import_log` is dropped once the session reports
  ready. `--debug` restores the indented form with every field.

## Command map (what the MCP tool names do)

| Task | Command | MCP equivalent |
| --- | --- | --- |
| target / daemon / session state | `"$PYA" status --json` | `pyaether_status` |
| find an API | `"$PYA" api search QUERY [--limit N] [--kind K] --json` | `pyaether_api_search` |
| read one signature | `"$PYA" api show SYMBOL [--max-chars N] --json` | `pyaether_api_help` |
| run Python in the resident session | `"$PYA" exec -c 'CODE' [--timeout S] --json` (stdin, or `-f FILE`) | `pyaether_exec` |
| SPICE netlist | `"$PYA" sim run NETLIST [--backend ngspice\|spectre\|alps\|custom] [--mode M] [--timeout S] --json` | `pyaether_sim_run` |
| layout build | `"$PYA" layout gen SPEC -o OUT.gds` | `pyaether_layout_gen` |
| layout read | `"$PYA" layout info FILE [--layers '{"m1":[1,0]}'] --json` | `pyaether_layout_info` |
| layout checks | `"$PYA" layout drc FILE --rules RULES.json --json` | `pyaether_layout_drc` |
| layout algebra | `"$PYA" layout boolean --op and --a m1 --b m2 --out-layer m3` | `pyaether_layout_boolean` |
| layout convert / compare | `"$PYA" layout convert SRC OUT.oas` / `layout compare A.gds B.gds` | `pyaether_layout_convert` / `_compare` |
| a real rule deck | `"$PYA" layout deck DECK.drc --source FILE.gds --json` | `pyaether_layout_deck` |
| schematic build / netlist | `"$PYA" sch build SPEC` / `sch netlist SPEC` | `pyaether_sch_build` / `_netlist` |
| schematic snapshot / round trip | `"$PYA" sch snapshot` / `sch roundtrip SPEC` | (CLI only) |
| profile state / assertion | `"$PYA" profile show --json` / `profile verify --json` | (CLI only) |

`"$PYA" <cmd> --help` is authoritative for flags. Long jobs (`sim run`, `layout
deck`, big `layout gen`) belong in the shell tool's **background** mode rather
than a long foreground timeout.

## Rules that keep results honest

1. **`ok` is the contract** — see above.
2. **Look up before calling**: `api search` then `api show`, instead of guessing a
   `pyAether` symbol or its argument order. The module name is lowercase
   `pyAether`; call `pyAether.emyInitDb()` before OpenAccess work.
3. **A DRC run with violations is not a failed check.** Read `clean` and the
   per-rule counts; a rule that could not be evaluated is an `error` on that rule
   and fails the whole run.
4. **Layers by name need a map.** GDS2 stores no layer names; pass
   `--layers '{"m1":[1,0]}'`. A name that cannot be resolved is an error, never
   "0 violations".
5. **A deck that runs but writes no report is `PARTIAL`** — read the deck's exit
   code and report database before claiming "design rule clean".
6. **Targets are independent.** PyAether, the simulator and KLayout may each run
   somewhere different (container / server / this machine). If a tool reports a
   missing binary, check which target was resolved before installing anything.

## Which target, and how it is asserted

A profile is a named target set in `$PYAETHER_BRIDGE_HOME/config.json` (default
`~/.cache/pyaether-bridge/config.json`), chosen by `PYAETHER_PROFILE` > nearest
`.pyaether-profile` > `default_profile`.

```bash
"$PYA" profile show --json      # active profile, its source, resolved paths, fingerprint
"$PYA" profile verify --json    # assert expected_* against the real target
```

* `expected_hostname` / `_container` / `_image` / `_aether_version` /
  `_license_server` in a profile are **assertions**: `verify` reports each as
  match / mismatch / unverifiable, and a declared value the target cannot report
  counts as **failure**, not as success.
* The first write into a live session runs the same check and refuses on a
  mismatch unless `PYAETHER_ALLOW_IDENTITY_MISMATCH=1`.
* With `PYAETHER_REQUIRE_EXPLICIT_PROFILE=1`, target-acting commands must name the
  profile in this process (`PYAETHER_PROFILE=…`). A binding file or a
  `default_profile` selects a target but does **not** confirm it for the command.
* Every reply carries a target fingerprint. If the profile's target changed while
  a daemon kept running, session calls fail on purpose; the fix is
  `"$PYA" daemon restart`.
* `aether_version` in a profile keys the symbol catalog to that release
  (`profiles/<name>/catalog-<version>.sqlite`); declaring it is what keeps two
  releases from answering each other's queries.

## Failure handling

* A missing binary (`ngspice`, `spectre`, `alps`, `klayout`) is reported as "not
  found on target" together with the resolved target name. Do not install
  anything to work around it without asking.
* A licence error is an environment problem, not a bridge bug. The bridge never
  reads, alters or works around licensing.
* One resident session equals one licence seat, and it starts lazily on the first
  `exec`. Do not keep sessions alive that nothing is using.
* `work/` and `data/` are local artifacts (the catalog is built from vendor docs
  and must stay out of git).
