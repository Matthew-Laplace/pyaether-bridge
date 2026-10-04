# Architecture and internal interfaces

All host-side code uses the **Python 3.9 standard library only** and pulls in no
third-party dependencies; the repository root is written as `<ROOT>`. For the
big picture see the [README](README.md#architecture).

## Layout

```
<ROOT>/pyaether_bridge/__init__.py         # version string
<ROOT>/pyaether_bridge/config.py           # configuration resolution (env var / user config file / default)
<ROOT>/pyaether_bridge/transports.py       # transports: docker / ssh / local
<ROOT>/pyaether_bridge/simulators.py       # simulator backends + result contract
<ROOT>/pyaether_bridge/catalog.py          # offline API catalog (sqlite + FTS5)
<ROOT>/pyaether_bridge/runtime.py          # host-side client (unix socket -> daemon)
<ROOT>/pyaether_bridge/daemon.py           # host daemon (serializes target requests)
<ROOT>/pyaether_bridge/session_bridge.py   # target-side resident executor (NDJSON over stdio)
<ROOT>/pyaether_bridge/cli.py              # command line entry point
<ROOT>/pyaether_bridge/mcp_server.py       # MCP stdio server
<ROOT>/pyaether_bridge/layout.py           # KLayout layer (gen/info/drc/boolean/tools/convert/compare/deck)
<ROOT>/pyaether_bridge/klayout_script.py   # target-side KLayout script, deployed by layout.py
<ROOT>/pyaether_bridge/schematic.py        # schematic round trip: canvas <-> Aether
<ROOT>/pyaether_bridge/sch_build_script.py # target-side: snapshot -> real schematic
<ROOT>/pyaether_bridge/sch_snapshot_script.py # target-side: real schematic -> snapshot
<ROOT>/pyaether_bridge/dsh.py              # DeepSeek Harness: workspace skill + wiring check
<ROOT>/bin/pyaether                        # CLI launcher shim
<ROOT>/bin/pyaether-mcp                    # MCP launcher shim
<ROOT>/tests/smoke_cli.sh                  # CLI smoke test (needs no Docker)
<ROOT>/tests/mcp_probe.py                  # MCP protocol and tool probe
<ROOT>/tests/transport_probe.py            # transport probe (local + fake ssh stub + optional docker)
<ROOT>/tests/simulator_probe.py            # simulator probe (real ngspice + contract checks)
<ROOT>/tests/layout_probe.py               # KLayout layer probe
<ROOT>/tests/schematic_probe.py            # schematic round-trip probe
<ROOT>/tests/profile_probe.py              # target profile + vendor backend probe
<ROOT>/tests/dsh_probe.py                  # DeepSeek Harness integration probe
<ROOT>/tests/alps_live_probe.py            # vendor simulator live probe (needs ALPS + a licence)
<ROOT>/integrations/dsh/skills/pyaether-bridge/SKILL.md  # model-facing tool guide, installed by dsh.py
<ROOT>/data/catalog.sqlite                 # local build artifact, not in git
```

## Request path

```
CLI / MCP ──unix socket(NDJSON)──► daemon ──transport(NDJSON)──► session_bridge
                                     │                                │
                              serialized by one mutex        resident pyAether session
```

- The daemon owns the session handle; the session runs `import pyAether` only on
  first use.
- On the target, fd 1/2 are redirected into temp files for the duration of each
  request so that OpenAccess/pyAether C-level output is captured instead of
  corrupting the protocol stream.
- One session equals one license seat, and there is only ever one session, which
  matches OpenAccess's single-writer constraint.

## transports.py

"Where does PyAether live" is abstracted behind one interface, so neither the
daemon nor the catalog calls docker/ssh directly any more:

```python
class Transport:
    def describe(self) -> dict            # state for `status`
    def label(self) -> str                # e.g. "docker:empyrean-gui" / "ssh:user@host"
    def probe(self) -> dict               # reachability + target interpreter path
    def write_file(path, data)            # deploy session_bridge.py
    def open_session(inner_command)       # start a persistent stdio session (Popen)
    def fetch_file(path, local_path)      # fetch a file back (api sync-live)
```

| Implementation | Deploy | Start a session | Fetch a file |
| --- | --- | --- | --- |
| `DockerTransport` | `docker exec -i <ctr> sh -c 'mkdir -p .. && cat > ..'` | `docker exec -i -w /tmp <ctr> bash -lc <cmd>` | `docker cp` |
| `SSHTransport` | `ssh -T -o BatchMode=yes <host> bash -lc '<cmd>'` | same, stdio straight through | `ssh <host> bash -lc 'cat <path>'` |
| `LocalTransport` | write a local file | local `bash -lc <cmd>` | `shutil.copyfile` |

Notes:

- All three implementations start through a **login shell**, because Aether's
  environment variables come from the profile.
- SSH must quote the whole remote command as one string (ssh joins argv with
  spaces and hands it to the remote login shell to parse); the fake ssh stub in
  `tests/transport_probe.py` reproduces that faithfully.
- The target's `LM_LICENSE_FILE` is overridden only when `PYAETHER_LICENSE_SERVER`
  is set explicitly.
- No credentials are read or stored; SSH authentication is left to ssh itself and
  `BatchMode` is forced so a background call never blocks on a password.

## catalog.py

```python
def build_from_html(docs_html_dir, out_db, *, jobs=1, progress=None) -> dict
    # -> {"entries": int, "pages": int, "db": str, "by_kind": {...},
    #     "by_domain": {...}, "elapsed_s": float, "skipped": int}
def sync_from_runtime(*, db_path=None, timeout=300.0, container=None) -> dict
    # merge dir(pyAether) symbols from the live session into the catalog (kind="runtime")
def search(query, *, limit=20, kind=None, module=None, db_path=None) -> list[dict]
    # each hit: {"name","kind","module","signature","summary","domain","page","anchor"}
    # ranking: exact name > prefix > substring > FTS5 term hit; doc/label rows
    # never outrank real API symbols
def show(name, *, max_chars=4000, db_path=None) -> dict | None
    # {"name","kind","module","signature","summary","description",
    #  "params","returns","page","anchor","domain"}
def stats(*, db_path=None) -> dict
```

Database schema (`schema_version=1`):

```sql
CREATE TABLE entries(
  name TEXT PRIMARY KEY, kind TEXT, module TEXT, signature TEXT,
  summary TEXT, description TEXT, params TEXT, returns TEXT,
  page TEXT, anchor TEXT, domain TEXT);
CREATE VIRTUAL TABLE entries_fts USING fts5(
  name, module, signature, summary, description, kind, domain);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
-- meta: entries, pages, built_at, source_dir, schema_version,
--       runtime_symbols, runtime_synced_at
```

`params`/`returns` are stored as JSON strings. A missing database raises
`CatalogMissingError` and points at `pyaether api build`.

## runtime.py

## simulators.py

Simulator-agnostic netlist execution. It reuses the transport layer, so a
simulator can run wherever a transport can reach -- including a different
machine than PyAether (`PYAETHER_SIM_TARGET`).

```python
def backends() -> dict                     # static description of every backend
def probe(backend, *, transport=None)      # is the binary there, and which version
def build_command(backend, *, netlist, workdir, log, raw, mode=None, args=None)
    # -> (shell command, extra environment); validates the mode before running
def resolve_target(backend=None) -> (kind, reason)
def transport_for_simulator(backend=None)  # PYAETHER_SIM_TARGET, else per-backend rule
def run(netlist, *, backend=None, transport=None, workdir=None, timeout=None,
        mode=None, args=None, includes=None, netlist_text=None, run_id=None) -> dict
def parse_ascii_raw(text) -> list[dict]    # SPICE ASCII rawfile -> plots
def parse_psf_ascii(text) -> dict          # PSF-ASCII scalar/vector VALUE records
def scalar(data, key) / vector(data, key)  # strict accessors, raise on bad input
```

`run()` returns `{"ok", "status", "backend", "data", "errors", "warnings",
"metadata"}` where `status` is `SUCCESS`, `PARTIAL` (the tool exited cleanly but
nothing was parseable) or `FAILURE`, and `metadata` carries the command, work
directory, artifact list, parsed plot headers and per-phase timings.

Layout of one run -- staging, execution and parsing are separate phases so a
slow remote copy is visible in `metadata["timings"]`:

```
<SIM_WORKDIR>/run-<backend>-<run_id>/
    <netlist>            staged (copied or supplied as text)
    <includes...>        staged next to the netlist
    sim.log              simulator log (ngspice -o, Spectre +log)
    raw.out or raw/      waveform data (ASCII rawfile, or PSF-ASCII)
```

Backends:

| Backend | Command shape |
| --- | --- |
| `ngspice` | `ngspice -b -o <log> -r <raw> <netlist>` with `SPICE_ASCIIRAWFILE=1` so results are text |
| `spectre` | `spectre -64 <netlist> +escchars +log <log> -format psfascii -raw <raw> <mode flags> +lqtimeout 900 -maxw 5 -maxn 5 +logstatus` |
| `custom` | `PYAETHER_SIM_CMD` with `{netlist}` `{workdir}` `{log}` `{raw}` `{mode}` substituted |

Spectre mode flags match `Arcadia-1/virtuoso-bridge-lite` (`+aps`, `+x`,
`+preset=cx|ax|mx|lx|vx` with `+mt`) so results from either bridge are
comparable. Details and usage: [docs/SIMULATORS.md](docs/SIMULATORS.md).

Target resolution (`resolve_target`) when `PYAETHER_SIM_TARGET` is unset: an
open-source backend runs on **this machine** if its binary is installed here,
because it needs no licence and is not part of the EDA installation (the Aether
container typically has no ngspice); `spectre` and `custom` follow the bridge
target. The applied rule is reported as `metadata.target_reason`.

```python
def request(method, params=None, *, timeout=30.0, autostart=True) -> dict
def exec_code(code, *, timeout=120.0, autostart=True) -> dict
    # -> {"ok","stdout","stderr","result_repr","result_json","elapsed_s",
    #     "timed_out","error","error_type"}
def status(*, autostart=False) -> dict
def ensure_daemon(*, wait=90.0) -> dict
def stop_daemon() -> dict
```

When the daemon is not running `request()` starts it automatically; set
`PYAETHER_BRIDGE_NO_AUTOSTART=1` to forbid that. A connection failure is retried
once and only when **no request has been sent yet**, so `exec_code` is never
silently replayed.

## layout.py

KLayout work stays out of process, which keeps the standard-library-only rule
intact and KLayout a separate, user-installed tool. `layout.py` builds a KLayout
script (`klayout_script.py`), deploys it over the transport, runs
`klayout -b -r <script>` with the `pya` module KLayout ships, and reads back a
JSON report -- nothing is parsed from stdout, so KLayout's log noise can never be
mistaken for a result.

```python
def probe() -> dict                        # is KLayout there, and which version
def generate(spec, *, output, timeout)     # spec -> GDS2/OASIS
def info(path, *, layers)                  # cells, layers, shape counts, extents
def drc(path, rules, *, layers)            # width/space/notch/enclosing/area
def boolean(op, a, b, ...)                 # merge/and/not/xor/size
def tools()                                # KLayout's standalone stream tools
def convert(source, output, *, tool)       # strm2oas/strm2gds/strm2cif/strmclip/...
def compare(a, b, *, tool)                 # strmcmp / strmxor
def deck(script, *, source, top, define)   # a real .drc / .lvs rule deck
```

Layer names are resolved through a caller-supplied map, because GDS2 stores no
layer names; a name that cannot be resolved is an error, never "0 violations". A
deck that runs but writes no report database is reported as `PARTIAL`, not as
"design rule clean".

## schematic.py

The round trip between a canvas drawing and a real schematic. The interchange
format is the `analog-agent.schematic` snapshot (instances / nets / terminals),
which the forward Aether -> canvas importers already speak, so both directions
exist: `build` creates a schematic through pyAether's own creation API
(`emyBlock.create`, `emyScalarInst.create`, `emyScalarNet.create`,
`emyInstTerm.create`, `emyTerm.create`, then `design.save`), and `snapshot` reads
one back out.

- The spec and the report travel as files, so no user text is ever interpolated
  into generated code.
- A missing target library is created with `dbCreateLib`; an existing cell is
  never overwritten -- each already-existing instance is reported instead.
- `netlist` emits SPICE offline (no session needed) and states in the header
  whether the deck is positional or a connectivity edge list, rather than
  silently shuffling nodes when pin order is unknown.
- Target-side helpers: `sch_build_script.py`, `sch_snapshot_script.py`.

## dsh.py

DeepSeek Harness integration, split into the two halves the harness actually has:
a *bundle* registers the MCP server, and a *skill* teaches the model to drive it.
This module owns the skill; the bundle is a separate local package
(`dsh-bundle-pyaether-bridge`) installed through the harness' plugin manager.

```python
def find_workspace(explicit=None, start=None) -> str   # --workspace / $DSH_WORKSPACE / .dsh-workspace marker
def skill_source() -> str                             # integrations/dsh/skills/<name>/SKILL.md
def skill_target(workspace) -> str                    # <workspace>/.dsh/skills/<name>/SKILL.md
def skill_root_state(workspace, home=None) -> dict    # which profiles scan / disable that root
def bundle_state(workspace, home=None) -> dict        # is the bundle there, which profiles declare it
def status(workspace=None, home=None) -> dict         # read-only view of all of the above
def install(workspace=None, home=None, dry_run=False) # copy the skill; never touches a profile
def uninstall(workspace=None, home=None)              # remove the skill again
```

- **It never writes to a profile.** A profile the desktop application manages
  refuses CLI composition (`error: profile "desktop" is managed exclusively by the
  Electron application`), so a patch written here can never be verified and has to
  be rolled back; an installer that always rolls back is worse than none. The
  bundle channel does the same job without editing the profile.
- **The skill is workspace-level, not global.** The filesystem skill provider
  ships disabled and then reads an explicit `customSkillDirs` list, so
  `$DSH_HOME/skills` is never scanned -- a skill written there is installed and
  invisible. `skill_root_state` reads the provider row back (is the root listed,
  is that row `disabled: true`), so `status` and `install` can report "installed
  but never loaded" instead of a success that means nothing.
- **The workspace root is the one the instruction loader pins**: the nearest
  ancestor carrying a `.dsh-workspace` marker, overridable with `--workspace` or
  `$DSH_WORKSPACE`.
- The provider row is read with small helpers (`_row_body`, `_row_disabled`)
  rather than a YAML parser, keeping the standard-library-only rule.

## Daemon and session protocol

Both hops speak NDJSON (one JSON object per line).

Daemon methods: `ping`, `status`, `exec`, `namespace`, `restart`, `stop`.

Session methods (`session_bridge.py`): `ping`, `status`, `exec`, `namespace`,
`shutdown`, `restart_session`.

A handshake message is sent first:

```json
{"type": "ready", "ok": true, "pid": 123, "python": "3.9.0",
 "cwd": "/tmp", "import_seconds": 8.03, "symbols": 16781, "import_log": "..."}
```

`ok=false` means `import pyAether` failed on the target (for example PyAether is
not installed there at all). The session still works for plain Python, which is
what lets you tell "the transport is broken" apart from "the environment is not
set up".

`exec` takes `{"code": str, "timeout": number}` and returns:

```json
{"ok": true, "stdout": "...", "stderr": "...", "result_repr": "...",
 "result_json": null, "elapsed_s": 0.02, "timed_out": false,
 "error": null, "error_type": null, "generation": 5, "namespace_keys": ["pyAether"]}
```

The session namespace survives across `exec` calls (handy for incremental work
such as `db = pyAether.pyaedb`).

## Configuration resolution

`config.setting(env, key, default)` resolves in four steps: **environment
variable -> active profile -> user config file -> default**. The user config
file is `$PYAETHER_BRIDGE_HOME/config.json` (default
`~/.cache/pyaether-bridge/config.json`) with the same snake_case keys used in
the docs. That keeps local paths, container names and license endpoints out of
the repository.

### Profiles

A profile is a named set of the same keys, so one checkout can talk to several
installations. `config.resolve_profile()` picks the active one from:

1. `PYAETHER_PROFILE`
2. the nearest `.pyaether-profile` file (searched upwards from the current
   directory, `PROFILE_SEARCH_DEPTH` levels); `pyaether profile bind <name>`
   writes one
3. `default_profile` in the config file
4. none -- top-level config keys only

When a profile is active `RUNTIME_DIR` becomes `<data dir>/profiles/<name>`, so
the daemon socket, startup lock, pid file and log are scoped to it: two profiles
can never hand out the same session. Without a profile the paths are exactly what
they were before, so existing setups keep working.

### Target-side paths are namespaced too

`config.scoped_path()` appends `-<profile>` to the *defaults* of `REMOTE_DIR`,
`SIM_WORKDIR`, `LAYOUT_WORKDIR` and `SCHEMATIC_WORKDIR`. A shared
`/tmp/pyaether-bridge` would let one profile overwrite another's deployed
`session_bridge.py`, and would mix staged netlists and layouts between targets.
An explicitly configured value is never rewritten, and with no active profile the
paths keep their historical values.

### Version-dependent artifacts

The symbol catalog is built from one installation's docs, so it is a
version-dependent artifact:

```python
AETHER_VERSION = setting("PYAETHER_AETHER_VERSION", "aether_version")
CATALOG_DB     = catalog_db_path()   # env > profile > per-(profile, version) file > repo default
```

Declaring `aether_version` keys the catalog to
`<data dir>/profiles/<name>/catalog-<version>.sqlite`; without it the historical
single catalog is kept. `resolve_docs_dir()` is the strict sibling of
`find_docs_dir()`: a pinned `docs_dir` must be usable, and when several installed
Aether trees are present and nothing is pinned it raises `ConfigError` naming the
installations instead of picking one. `api build` uses the strict form. A
`docs_dir` inside a profile block is honoured -- it used to be read from the
top-level config only, so every profile silently built from the same
installation's docs.

### Target identity

A profile names a target; `expected_*` values assert *which* target it is:

```python
IDENTITY_FIELDS = ("hostname", "container", "image", "aether_version", "license_server")
EXPECTED_IDENTITY = {field: setting("PYAETHER_EXPECTED_<FIELD>", "expected_<field>")}
```

`daemon.observed_identity()` gathers what the target reports (the transport probe
plus one SSH `hostname` call), `identity_report()` classifies each field as
`match` / `mismatch` / `unverifiable` / `unconfigured`, and the daemon runs the
check once before the first `exec` into the live session. Two deliberate choices:

* a *declared* expectation that cannot be observed is **blocking**, not a pass --
  "could not verify" is exactly the state in which a wrong target slips through;
* `PYAETHER_ALLOW_IDENTITY_MISMATCH=1` turns a mismatch into a warning, so an
  intentional cross-target session stays possible.

`profile verify` runs the same check on demand and reports the truth even when
the override is set; the override only decides whether *writes* are refused.

### Explicit-profile guard and the target fingerprint

`PYAETHER_REQUIRE_EXPLICIT_PROFILE=1` makes commands that act on a target require
the profile to be named by this process. `config.profile_is_confirmed()` is true
only for `PYAETHER_PROFILE`: a binding file or `default_profile` selects a target
but is not a confirmation for the current command. `runtime.exec_code()` enforces
it for `exec` (shared with the MCP server) and `cli.WRITE_COMMANDS` covers the
rest; read-only commands are never blocked.

Every daemon reply carries `config.TARGET_FINGERPRINT`, a hash of the resolved
target settings. The client checks it on session methods (`exec`, `namespace`)
and refuses with `BridgeStaleDaemon`: a daemon started for another target must not
answer a session call, and the message names `daemon restart` as the fix. Control
methods (`ping`, `status`, `stop`, `restart`) deliberately skip the check --
otherwise the user could not stop the very daemon they were told to restart.
`status` reports the comparison as data instead of raising.

## CLI

```
pyaether status [--json]
pyaether exec [-c CODE | -f FILE] [--timeout S] [--json]     # reads stdin without -c/-f
pyaether api build [--docs DIR] [--out DB] [--jobs N] [--json]
pyaether api stats [--json]
pyaether api search QUERY [--limit N] [--kind K] [--db DB] [--json]
pyaether api show SYMBOL [--max-chars N] [--db DB] [--json]
pyaether api sync-live [--db DB] [--timeout S] [--json]
pyaether daemon [start|stop|status|restart] [--json]
pyaether profile [list|show|bind NAME|clear|verify] [--json]
pyaether sim backends [--probe] [--json]
pyaether sim run NETLIST [--backend ngspice|spectre|custom] [--mode MODE]
                        [--timeout S] [--include FILE] [--run-id ID] [--json]
pyaether layout probe|gen|info|drc|boolean|tools|convert|compare|deck [--json]
pyaether sch snapshot|build|netlist|roundtrip [--json]
pyaether dsh status|install|uninstall [--workspace DIR] [--dsh-home DIR]
                        [--dry-run] [--json]
pyaether version
```

Exit codes: `0` success; `1` runtime failure (exec raised, catalog missing, ...);
`2` usage error. Human-readable output is English; `--json` prints raw JSON.
`sim run` exits `1` whenever the simulation result is not `ok`.
`profile verify` exits `1` when a declared identity mismatches or cannot be
verified, and `0` when nothing is declared. A command refused by
`PYAETHER_REQUIRE_EXPLICIT_PROFILE` exits `1`.

## MCP server

Single stdio process, NDJSON JSON-RPC 2.0 (protocol version `2024-11-05`).
**stdout carries protocol messages only**; all diagnostics go to stderr.

Methods: `initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`.
Unknown method `-32601`, unknown tool `-32602`, a bad JSON line `-32700` (the line
is dropped and the server keeps serving). A tool that fails internally answers
with `{"content": [...], "isError": true}` instead of a protocol error.

| Tool | inputSchema |
| --- | --- |
| `pyaether_status` | `{}` |
| `pyaether_api_search` | `{query: string, limit?: integer, kind?: string}` |
| `pyaether_api_help` | `{symbol: string, max_chars?: integer}` |
| `pyaether_exec` | `{code: string, timeout?: number}` |
| `pyaether_sim_run` | `{netlist: string, backend?: string, mode?: string, timeout?: number}` |
| `pyaether_layout_gen` | `{spec: object, output?: string, timeout?: number}` |
| `pyaether_layout_info` | `{path: string, layers?: object, timeout?: number}` |
| `pyaether_layout_drc` | `{path: string, rules: array, layers?: object, timeout?: number}` |
| `pyaether_layout_boolean` | `{op: string, a: string, b?: string, value?: number, out_layer: string, source?: string, output?: string, layers?: object, timeout?: number}` |
| `pyaether_layout_convert` | `{source: string, output: string, tool?: string, timeout?: number}` |
| `pyaether_layout_compare` | `{a: string, b: string, tool?: string, timeout?: number}` |
| `pyaether_layout_deck` | `{script: string, source?: string, top?: string, define?: array, timeout?: number}` |
| `pyaether_sch_build` | `{spec: object, library?: string, cell?: string, view?: string, timeout?: number}` |
| `pyaether_sch_netlist` | `{spec: object, subckt?: string, schematic_library?: string, cell?: string}` |

The schemas are defined once in `TOOLS` (`pyaether_bridge/mcp_server.py`, 14
tools); the model-facing summary of when to use each one is
`integrations/dsh/skills/pyaether-bridge/SKILL.md`.
