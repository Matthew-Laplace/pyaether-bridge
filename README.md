# pyaether-bridge

**English** | [日本語](README.ja.md)

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
./bin/pyaether version
```

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
`pyaether_exec`. Writing to a real `config.toml` changes your user
configuration, so this repository does not modify it for you.

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

- **One license seat.** The resident session holds a `PY_AETHER` seat for as
  long as it runs; a second independent PyAether process may fail to check one
  out.
- **Serialized OA access.** Every request goes through a single daemon lock.
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

## Tests

```bash
bash tests/smoke_cli.sh                  # CLI smoke test, needs no Docker/daemon
python3 tests/mcp_probe.py               # MCP protocol + 4 tools, 14 checks
python3 tests/transport_probe.py         # transport layer: local + fake-ssh stub
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # also exercise a real container
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — target preparation, self-checks, troubleshooting
- [ARCHITECTURE.md](ARCHITECTURE.md) — module layout, internal interfaces, wire format
- [README.ja.md](README.ja.md) — 日本語版

## License

MIT — see [LICENSE](LICENSE).
