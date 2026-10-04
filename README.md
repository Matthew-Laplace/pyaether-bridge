# pyaether-bridge

**English** | [日本語](README.ja.md) | [繁體中文](README.zh-TW.md)

Bridge Empyrean Aether / PyAether into a command line tool and an MCP server.
A resident daemon keeps one pyAether session alive on the target, and an offline
API catalog makes ~28k symbols searchable. Pure Python 3.9 standard library —
no third-party dependencies.

**Wherever PyAether lives, there is a transport for it** (same CLI and MCP
server, one environment variable apart):

| Where PyAether is installed | `PYAETHER_TRANSPORT` | How the bridge gets in |
| --- | --- | --- |
| Inside a Docker container | `docker` (default) | `docker exec` |
| On a company / lab server | `ssh` | `ssh`, using your existing config and keys |
| On the same Linux box as the bridge | `local` | plain local process |

> **Unofficial project.** Not affiliated with, endorsed by, or supported by
> Empyrean Technology. This repository contains and distributes no vendor
> software, binaries, or documentation. You need your own **licensed** Aether
> installation and license. See [docs/INSTALL.md](docs/INSTALL.md) for target
> preparation.
>
> If Empyrean Technology (华大九天) believes that this project infringes any of
> its rights, please [open an issue](https://github.com/Matthew-Laplace/pyaether-bridge/issues)
> and we will modify or remove the material in question as soon as possible.

```bash
git clone https://github.com/Matthew-Laplace/pyaether-bridge.git
cd pyaether-bridge
./bin/pyaether version
```

## Architecture

```
host process                 host daemon                target
------------                 -----------                ------
bin/pyaether  ┐                                         /tmp/pyaether-bridge/
bin/pyaether-mcp ├─ unix socket ─→ daemon ── transport ──→ session_bridge.py
MCP client     ┘   (NDJSON)      (single-writer lock)     resident pyAether session
                                               │          (imports pyAether once)
                      transport: docker exec / ssh / local bash
                                                                    │
offline API catalog data/catalog.sqlite ←── api build ←── Sphinx docs/html ┘
```

- The CLI and MCP server only talk to the daemon over a unix socket; they never
  touch Docker or SSH themselves.
- The daemon forwards requests to a resident Python session on the target. That
  session imports pyAether **once**; later snippets run in the same namespace,
  so variables persist across `exec` calls.
- All three transports start through a **login shell** (`bash -lc`), because
  Aether's `AETHER_*`, `LD_LIBRARY_PATH` and `PYTHONPATH` come from the image or
  server profile.
- OpenAccess writes are inherently single-process, so the daemon serializes
  every request behind one lock.
- The bridge never reads or stores credentials: SSH authentication is left to
  `ssh` itself (keys + ssh-agent recommended), and `BatchMode` is forced so a
  background call fails fast instead of hanging on a password prompt.

## Install

Nothing to install, no pip, no writes to system directories. Clone and call the
executables in the repository (all commands below assume the repository root is
the current directory):

```bash
./bin/pyaether version
```

Requirements:

- Host `python3` (tested on 3.9.6).
- Aether installed on the target (with its bundled Python 3.9 and PyAether) and
  able to check out a license.
- Whatever the chosen transport needs: the Docker CLI, or SSH access to the
  server.

## Connecting to your PyAether

**Docker (default)**

```bash
./bin/pyaether status                      # transport=docker, container empyrean-gui
export PYAETHER_CONTAINER=my-aether        # point it at your own container
```

**SSH to a server**

```bash
export PYAETHER_TRANSPORT=ssh
export PYAETHER_SSH_HOST=aether@lab-server # an alias from ~/.ssh/config works too
# optional: PYAETHER_SSH_PORT=2222, PYAETHER_SSH_OPTS="-o StrictHostKeyChecking=no"
./bin/pyaether status
```

The bridge deploys `session_bridge.py` to `PYAETHER_REMOTE_DIR` (default
`/tmp/pyaether-bridge`) on the server and starts the resident session there.
Nothing needs to be installed on the host. If the interpreter is not named
`python3.9`, set `PYAETHER_PYTHON`.

**Local (the bridge runs on the same machine as Aether)**

```bash
export PYAETHER_TRANSPORT=local
./bin/pyaether exec -c 'import pyAether; print(pyAether.__file__)'
```

Any of these settings can live in `~/.cache/pyaether-bridge/config.json`
(see [Environment variables](#environment-variables)) instead of being exported
in every shell.

## CLI usage

```bash
./bin/pyaether status                 # target + daemon + session status
./bin/pyaether status --json          # raw JSON

./bin/pyaether daemon start           # start the host daemon (session starts lazily)
./bin/pyaether daemon status
./bin/pyaether daemon restart
./bin/pyaether daemon stop

./bin/pyaether exec -c 'pyAether.emyInitDb()'
./bin/pyaether exec -f script.py --timeout 300
echo 'import pyAether; print(pyAether.__file__)' | ./bin/pyaether exec

./bin/pyaether api build --docs ./data/docs --jobs 4
./bin/pyaether api stats
./bin/pyaether api search emyInitDb --limit 10
./bin/pyaether api show pyAether.emyInitDb

./bin/pyaether sim backends --probe            # which simulators are available
./bin/pyaether sim run rc.cir --backend ngspice  # open-source SPICE
./bin/pyaether sim run tb.scs --backend spectre --mode ax

./bin/pyaether layout gen spec.json -o out.gds    # KLayout: build a layout
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout compare a.gds b.gds         # exit 0 = identical

./bin/pyaether dsh install --profile <profile>    # DeepSeek Harness plugin
./bin/pyaether version
```

## Layout (KLayout)

Layouts are generated, read, checked and converted with **KLayout**, driven the
way its authors support headless use: `klayout -b -r <script>` (`-b` is
`-zz -nc -rx`, so no GUI, no configuration file, no implicit macros). No Python
package is installed -- the script runs inside KLayout's own interpreter -- and
KLayout is invoked as a separate process, never imported.

```bash
./bin/pyaether layout probe                        # available? which version?
./bin/pyaether layout gen spec.json -o out.gds     # boxes, polygons, paths, texts, arrays
./bin/pyaether layout info out.gds --layers '{"m1":[1,0]}'
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout boolean and --a 1/0 --b 2/0 --out-layer 4/0 --source out.gds -o and.gds
./bin/pyaether layout convert out.gds out.oas      # KLayout's own stream tools
./bin/pyaether layout compare a.gds b.gds          # exit 0 = identical
./bin/pyaether layout deck rules.drc --source out.gds
```

Three behaviours are deliberate: a DRC run with violations exits `3` (a dirty
layout must not pass CI), touching edges from merged geometry are not counted as
spacing errors, and a rule that cannot be evaluated fails the run instead of
reporting zero violations. See [docs/LAYOUT.md](docs/LAYOUT.md).

## DeepSeek Harness

The bridge is an MCP server, and DeepSeek Harness ships an MCP client plugin, so
it installs as a DSH plugin with one command:

```bash
./bin/pyaether dsh install --profile <profile>   # patch entry + skill, with a backup
./bin/pyaether dsh verify --profile <profile>    # prove the harness composes it
```

`--profile` defaults to `web`: pass the profile you actually run (`echo
$DSH_PROFILE`). The patch entry is per profile, the skill is global.

The install raises the MCP client's per-call timeout (its 60 s default is too
short for simulations and layout jobs), uses the `- insert:` form the harness
requires, and rolls the edit back if the harness rejects the result. See
[docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md).

## Simulators

Netlists are not tied to one simulator: the same file can be checked with
open-source ngspice and signed off with Cadence Spectre/APS, and both come back
through the same result contract (`ok` / `status` / `data` / `errors` /
`metadata`). The simulator target is selected **independently** of the PyAether
target, so ngspice can run on your laptop while PyAether lives in a container.

| Backend | Kind | Modes |
| --- | --- | --- |
| `ngspice` | open source | analysis declared in the netlist |
| `spectre` | commercial | `spectre`, `aps`, `x`, `cx`, `ax`, `mx`, `lx`, `vx` |
| `alps` | commercial (Empyrean) | `basic`, `turbo`, `pro` |
| `custom` | any simulator | your own command template |

```bash
# open-source engines prefer this machine automatically when the binary is here;
# pin it explicitly if you want to be sure:
export PYAETHER_SIM_TARGET=local
./bin/pyaether sim run rc.cir --backend ngspice --json

# any other tool, including an in-house or vendor simulator
export PYAETHER_SIM_CMD='Xyce -l {log} -r {raw}.raw {netlist}'
export PYAETHER_SIM_ENV='SPICE_ASCIIRAWFILE=1'   # when the tool defaults to binary output
./bin/pyaether sim run rc.cir --backend custom
```

`ok` is the execution contract: a non-empty `data` is never treated as proof of
success, failures are classified (netlist read error / license error /
convergence failure / missing file / crash), and the strict accessors refuse to
invent numbers. See [docs/SIMULATORS.md](docs/SIMULATORS.md).

## One machine, several targets (profiles)

The same checkout can talk to several installations without editing anything:
a profile is a named group of settings in the user config file, and a
``.pyaether-profile`` file binds a directory to one of them.

```bash
# ~/.cache/pyaether-bridge/config.json
# {
#   "default_profile": "container",
#   "profiles": {
#     "container": {"transport": "docker", "container": "empyrean-gui"},
#     "lab":       {"transport": "ssh", "ssh_host": "aether@lab-server",
#                   "sim_backend": "alps", "sim_target": "ssh"}
#   }
# }

./bin/pyaether profile list          # what is defined, and which is active
./bin/pyaether profile show          # active profile + every resolved setting
./bin/pyaether profile bind lab      # bind this directory to "lab"
./bin/pyaether profile clear         # drop the binding
PYAETHER_PROFILE=lab ./bin/pyaether status   # or pick one for a single command
```

Resolution order for every setting is **environment variable > active profile >
top-level config > default**, and each profile gets its own daemon socket
(`<data dir>/profiles/<name>/daemon.sock`), so two profiles never share a
session by accident.

Conventions: human-readable output is English, `--json` prints raw JSON. Exit
code `0` for success, `1` for a runtime failure (daemon won't start, catalog
missing, `exec` raised, ...), `2` for a usage error. `exec` prints the executed
code's stdout first and then the value of the final expression; failures go to
stderr.

With `PYAETHER_BRIDGE_NO_AUTOSTART=1`, `status` reports the current state and
starts nothing.

## Registering the MCP server

`bin/pyaether-mcp` is a stdio MCP server (NDJSON JSON-RPC) that Codex can launch
directly:

```toml
# ~/.codex/config.toml — add only after you decide to change your own config
[mcp_servers.pyaether]
command = "/usr/bin/python3"
args = ["/absolute/path/to/pyaether-bridge/bin/pyaether-mcp"]
```

Tools exposed: `pyaether_status`, `pyaether_api_search`, `pyaether_api_help`,
`pyaether_exec`, `pyaether_sim_run`. Writing to a real `config.toml` changes
your user configuration, so this repository does not modify it for you.

## Offline API catalog

The catalog is built from the Sphinx documentation that ships **inside your own**
Aether installation (`objects.inv` plus the generated API pages). The documents
live at `$AETHER_ROOT/tools/pyaether/docs/html`; `api build` looks for
`$PYAETHER_DOCS_DIR`, then `docs_dir` from your user config, then `$AETHER_ROOT`,
then common install roots (`/opt/empyrean/*`, `~/empyrean/*`).

When Aether lives in a container, copy the docs out first (`data/` is
git-ignored):

```bash
CTR=empyrean-gui
DOCS=$(docker exec "$CTR" bash -lc 'ls -d "$AETHER_ROOT"/tools/pyaether/docs/html')
docker cp "$CTR:$DOCS" ./data/docs

# same idea over SSH
DOCS=$(ssh aether@lab-server 'echo "$AETHER_ROOT"/tools/pyaether/docs/html')
scp -r "aether@lab-server:$DOCS" ./data/docs

./bin/pyaether api build --docs ./data/docs --jobs 4   # writes data/catalog.sqlite
./bin/pyaether api sync-live                           # adds runtime-only symbols (needs daemon)
```

One database holds three kinds of rows; filter them with `--kind`:

| kind | rows (measured with 2026.03) | source |
| --- | --- | --- |
| `function` / `method` / `class` / `attribute` / `module` | 10376 | Sphinx `api_reference` from the installer |
| `runtime` | 12582 | `dir(pyAether)` symbols **not covered by the docs** |
| `doc` / `label` | 5274 | documentation section labels (always ranked below API symbols) |

28232 rows in total. **The docs do not cover every runtime symbol** (`emyInitDb`
exists only at runtime, for example), so run `api sync-live` after `api build`.
`sync-live` is repeatable: it clears its previous runtime rows and recomputes
them, never duplicating symbols the docs already describe, and search always
prefers the documented entry with the full typed signature.

> **Never commit or redistribute `data/catalog.sqlite`.** It is interface
> metadata extracted from vendor documentation (including description text) and
> should only be generated and used locally, against an installation you are
> licensed to use. `data/` is excluded in `.gitignore`; please keep it that way.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `PYAETHER_TRANSPORT` | `docker` (default) / `ssh` / `local` |
| `PYAETHER_CONTAINER` | Docker container name (default `empyrean-gui`) |
| `PYAETHER_SSH_HOST` | SSH target, e.g. `user@server` or an `~/.ssh/config` alias (required for `ssh`) |
| `PYAETHER_SSH_PORT` | SSH port (optional) |
| `PYAETHER_SSH_OPTS` | Extra ssh options, e.g. `-o StrictHostKeyChecking=no` (optional) |
| `PYAETHER_PYTHON` | Interpreter on the target (default `python3.9`, resolved via the login shell) |
| `PYAETHER_REMOTE_DIR` | Where the session script is deployed (default `/tmp/pyaether-bridge`) |
| `PYAETHER_LICENSE_SERVER` | Overrides the target's `LM_LICENSE_FILE`; unset keeps the target's own value |
| `PYAETHER_BRIDGE_HOME` | Daemon data directory (default `~/.cache/pyaether-bridge`) |
| `PYAETHER_DAEMON_SOCK` | Daemon unix socket path |
| `PYAETHER_CATALOG_DB` | Catalog sqlite path (default `data/catalog.sqlite` in the repo) |
| `PYAETHER_DOCS_DIR` | Default docs directory for `api build` |
| `PYAETHER_BRIDGE_NO_AUTOSTART` | Set to `1` to forbid auto-starting the daemon |
| `PYAETHER_SIM_TARGET` | Where simulators run: `docker` / `ssh` / `local` (default: the bridge transport) |
| `PYAETHER_SIM_BACKEND` | Default simulator backend (default `ngspice`) |
| `PYAETHER_SIM_WORKDIR` | Simulator work directory on the target (default `/tmp/pyaether-sim`) |
| `PYAETHER_SIM_TIMEOUT` | Default simulation timeout in seconds (default 600) |
| `PYAETHER_SIM_SSH_HOST` / `_PORT` / `_OPTS` | Simulator SSH target when it differs from the bridge target |
| `PYAETHER_SIM_CONTAINER` | Simulator container when it differs from the bridge container |
| `PYAETHER_SIM_CMD` | Command template for the `custom` backend (`{netlist}` `{workdir}` `{log}` `{raw}` `{mode}`) |
| `PYAETHER_SIM_ENV` | Extra environment for any simulator command, e.g. `SPICE_ASCIIRAWFILE=1` (comma-separated `KEY=VALUE`) |
| `PYAETHER_NGSPICE_BIN` | ngspice binary name or path (default `ngspice`) |
| `PYAETHER_SPECTRE_BIN` | Spectre binary name or path (default `spectre`) |
| `PYAETHER_ALPS_BIN` | Empyrean ALPS binary (default `alps`; needs your own valid licence) |
| `PYAETHER_ALPS_THREADS` | Threads passed to ALPS as `-mt` (optional) |
| `PYAETHER_PROFILE` | Active profile for this command (overrides the binding file) |
| `PYAETHER_KLAYOUT_BIN` | KLayout executable (default `klayout`) |
| `PYAETHER_KLAYOUT_TARGET` | Where KLayout runs: `docker` / `ssh` / `local` (default: this machine when KLayout is installed here) |
| `PYAETHER_KLAYOUT_BUDDY_DIR` | Directory holding KLayout's stream tools (default: inferred from the KLayout binary) |
| `PYAETHER_LAYOUT_WORKDIR` | Layout run directory root on the target (default `/tmp/pyaether-layout`) |
| `PYAETHER_LAYOUT_TIMEOUT` | Default layout timeout in seconds (default 600) |

Machine-specific settings can also live in
`$PYAETHER_BRIDGE_HOME/config.json` (default
`~/.cache/pyaether-bridge/config.json`) so local paths never enter the
repository:

```json
{
  "transport": "ssh",
  "ssh_host": "aether@lab-server",
  "ssh_port": "22",
  "python": "python3.9",
  "license_server": "port@host",
  "docs_dir": "/path/to/tools/pyaether/docs/html"
}
```

## Known limitations

- **Third-party rights.** This is an unofficial project: it ships no vendor
  software or documentation, and the API catalog is generated locally from your
  own licensed installation. If Empyrean Technology believes any material here
  infringes its rights, please
  [open an issue](https://github.com/Matthew-Laplace/pyaether-bridge/issues) and
  it will be modified or removed promptly.
- **One license seat.** The resident session holds a `PY_AETHER` seat for as
  long as it runs; a second independent PyAether process may fail to check one
  out.
- **Serialized OA access.** Every request goes through a single daemon lock, so a
  long-running `exec` blocks every other call until it finishes or times out.
  That suits debugging and batch jobs, not high-concurrency serving.
- **Target restarts.** After a container rebuild or server reboot the saved
  session dies; the next request rebuilds it automatically (re-importing
  pyAether, a few seconds) and variables from the old namespace are gone.
- **SSH authentication.** Only your existing SSH config / ssh-agent is used. The
  bridge forces `BatchMode`, so a call that would need an interactive password
  fails instead of hanging. Make sure `ssh <host>` works passwordless first.
- **Docs source for `api build`.** Needs `tools/pyaether/docs/html` from an
  installation you already have (or a copy you pulled out of the container).
  When it cannot find them it says so explicitly rather than failing silently.
- **The catalog is a read-only artifact.** Only `api build` rewrites the sqlite
  file; every other command is read-only.
- **Target-side Python only.** The bridge relies on Aether's bundled Python 3.9
  and OpenAccess shared libraries. Host-side code performs no EDA computation —
  it only forwards requests and searches the catalog.
- **The `ssh` transport has only been exercised against a stub.** Docker and
  local were validated end to end, and ssh against a stub that faithfully
  reproduces remote-shell semantics — but not yet against a real remote server.
  Treat the first real deployment as a test.
- **POSIX hosts only.** The daemon communicates over a unix domain socket, so
  the host running the CLI / MCP server must be macOS or Linux; native Windows
  is not supported.
- **Runtime-only symbols have weak signatures.** `runtime` entries come from
  `inspect` on SWIG objects and often read `(*args, **kwargs)`. The fully typed
  signatures live in the documentation entries, which is why search ranks those
  first.
- **Arbitrary code execution, by design.** `pyaether exec` and the MCP
  `pyaether_exec` tool run arbitrary Python in the target session with your own
  privileges — that is the point of the tool. The daemon's unix socket is mode
  `0600`, so only your user can reach it, but do not hand this bridge to
  untrusted callers.
- **Version and layout assumptions.** The target must expose the lowercase module
  `pyAether`, and `api build` expects the docs under `tools/pyaether/docs/html`.
  Development and testing used Aether 2026.03; other releases are untested.

## Tests

```bash
bash tests/smoke_cli.sh                  # CLI smoke test, needs no Docker/daemon
python3 tests/mcp_probe.py               # MCP protocol + every tool
python3 tests/transport_probe.py         # transport layer: local + fake-ssh stub
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # also exercise a real container
python3 tests/simulator_probe.py         # real ngspice run + result contract
python3 tests/profile_probe.py           # profile resolution and daemon isolation
python3 tests/layout_probe.py            # real KLayout runs against known geometry
python3 tests/dsh_probe.py               # DeepSeek Harness install/compose cycle
PYAETHER_ALPS_PROBE=1 python3 tests/alps_live_probe.py    # vendor simulator, needs a licence
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — target preparation, self-checks, troubleshooting
- [docs/SIMULATORS.md](docs/SIMULATORS.md) — simulator backends, result contract, adding a new one
- [docs/LAYOUT.md](docs/LAYOUT.md) — KLayout operations, spec and rule formats, licence boundary
- [docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md) — installing the bridge as a DSH plugin
- [ARCHITECTURE.md](ARCHITECTURE.md) — module layout, internal interfaces, wire format
- [README.ja.md](README.ja.md) — 日本語版
- [README.zh-TW.md](README.zh-TW.md) — 繁體中文版

## License

MIT — see [LICENSE](LICENSE).
